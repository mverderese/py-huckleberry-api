"""A stateless OAuth 2.1 authorization server where users sign in with their Huckleberry account.

Flow: Claude registers itself (DCR) and sends the user to /authorize. The SDK validates
the request and calls authorize(), which seals it and redirects to /login. The user
enters their Huckleberry email and password; the server signs in to Firebase once,
seals the resulting Firebase refresh token into an authorization code, and redirects
back to Claude. The token endpoint turns that into:

- an access token holding a short-lived Firebase ID token (what the tools use), and
- a refresh token holding the Firebase refresh token (used to mint new ID tokens).

The password is used once and never stored. Nothing is stored server-side; see sealing.py.
Anyone with a Huckleberry account can sign in; each person's tokens only reach their own
account. The time zone chosen on the sign-in page (prefilled from the browser) travels in
every token too, as that user's default for reading and writing times.

Limits of being stateless: an authorization code can be replayed until it expires
(5 minutes, and only together with the PKCE verifier), and an old refresh token keeps
working after rotation. Firebase has no endpoint for revoking a refresh token from the
client, so revocation can't end a sign-in early; rotating TOKEN_SECRET ends all of them.
"""

import base64
import hashlib
import html
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from . import firebase_auth
from .config import Settings
from .sealing import Sealer, fingerprint

SCOPE = "huckleberry"
AUTH_REQUEST_TTL = 15 * 60
CODE_TTL = 5 * 60
ACCESS_TTL_MAX = 60 * 60
# Access tokens expire this long before the ID token inside them, so a tool call never
# runs with an ID token that HuckleberryAPI would consider due for refresh (5 minutes).
ID_TOKEN_MARGIN = 10 * 60
REFRESH_TTL = 180 * 24 * 60 * 60

CLAUDE_CALLBACKS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
}


class HKAuthorizationCode(AuthorizationCode):
    fb_refresh_token: str
    uid: str
    timezone: Optional[str] = None


class HKRefreshToken(RefreshToken):
    fb_refresh_token: str
    uid: str
    timezone: Optional[str] = None


class HKAccessToken(AccessToken):
    id_token: str
    id_token_expires_at: float
    uid: str
    timezone: Optional[str] = None


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        return False
    return True


def redirect_uri_allowed(uri: str, allow_loopback: bool) -> bool:
    """Only Claude's hosted callback, plus loopback redirects for Claude Code."""
    if uri in CLAUDE_CALLBACKS:
        return True
    parsed = urlparse(uri)
    return allow_loopback and parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")


class StatelessOAuthProvider(OAuthAuthorizationServerProvider[HKAuthorizationCode, HKRefreshToken, HKAccessToken]):
    """Every OAuth artifact is sealed, so nothing is stored (identity assertion grants stay unsupported)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.sealer = Sealer(settings.token_secrets)

    # --- Clients -----------------------------------------------------------------

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        bad = [u for u in uris if not redirect_uri_allowed(u, self.settings.allow_loopback_redirects)]
        if not uris or bad:
            raise RegistrationError("invalid_redirect_uri", f"Redirect URI not allowed: {', '.join(bad) or 'none'}")
        # The SDK returns this same object to the client, so replacing the random id
        # with a sealed copy of the registration makes the client id self-describing.
        info = client_info.model_dump(mode="json", exclude={"client_id"}, exclude_none=True)
        client_info.client_id = self.sealer.seal("client", {"info": info}, ttl=None)

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        data = self.sealer.open("client", client_id)
        if not data:
            return None
        return OAuthClientInformationFull.model_validate({**data["info"], "client_id": client_id})

    # --- Authorization -------------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request = self.sealer.seal(
            "authreq",
            {
                "cid": fingerprint(client.client_id or ""),
                "client_name": client.client_name or "An MCP client",
                "redirect_uri": str(params.redirect_uri),
                "explicit": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "state": params.state,
                "scopes": params.scopes or [SCOPE],
                "resource": params.resource,
            },
            ttl=AUTH_REQUEST_TTL,
        )
        return f"{self.settings.base_url}/login?req={request}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[HKAuthorizationCode]:
        data = self.sealer.open("code", authorization_code)
        if not data or data["cid"] != fingerprint(client.client_id or ""):
            return None
        return HKAuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=data["exp"],
            client_id=client.client_id or "",
            code_challenge=data["code_challenge"],
            redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=data["explicit"],
            resource=data.get("resource"),
            subject=data["email"],
            fb_refresh_token=data["fb_rt"],
            uid=data["uid"],
            timezone=data.get("tz"),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: HKAuthorizationCode
    ) -> OAuthToken:
        return await self._issue_tokens(
            client,
            authorization_code.fb_refresh_token,
            authorization_code.uid,
            authorization_code.subject or "",
            authorization_code.scopes,
            authorization_code.resource,
            authorization_code.timezone,
        )

    # --- Refresh -----------------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[HKRefreshToken]:
        data = self.sealer.open("refresh", refresh_token)
        if not data or data["cid"] != fingerprint(client.client_id or ""):
            return None
        return HKRefreshToken(
            token=refresh_token,
            client_id=client.client_id or "",
            scopes=data["scopes"],
            expires_at=data["exp"],
            resource=data.get("resource"),
            subject=data["email"],
            fb_refresh_token=data["fb_rt"],
            uid=data["uid"],
            timezone=data.get("tz"),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: HKRefreshToken, scopes: List[str]
    ) -> OAuthToken:
        return await self._issue_tokens(
            client,
            refresh_token.fb_refresh_token,
            refresh_token.uid,
            refresh_token.subject or "",
            scopes or refresh_token.scopes,
            refresh_token.resource,
            refresh_token.timezone,
        )

    async def _issue_tokens(
        self,
        client: OAuthClientInformationFull,
        fb_refresh_token: str,
        uid: str,
        email: str,
        scopes: List[str],
        resource: Optional[str],
        tz: Optional[str],
    ) -> OAuthToken:
        try:
            session = await firebase_auth.refresh(fb_refresh_token, uid)
        except firebase_auth.AuthError:
            raise TokenError("invalid_grant", "Huckleberry sign-in expired; reconnect to sign in again") from None
        except firebase_auth.HuckleberryError as e:
            # Not the client's fault; there's no better OAuth error code for an upstream outage
            raise TokenError("invalid_request", f"Couldn't reach Huckleberry: {e}") from None

        id_token_ttl = int(session.expires_at - time.time())
        access_ttl = max(60, min(ACCESS_TTL_MAX, id_token_ttl - ID_TOKEN_MARGIN))
        common = {
            "cid": fingerprint(client.client_id or ""),
            "email": email,
            "uid": uid,
            "scopes": scopes,
            "resource": resource,
            "tz": tz,
        }

        access = self.sealer.seal(
            "access", {**common, "idt": session.id_token, "idx": session.expires_at}, ttl=access_ttl
        )
        # Firebase may rotate the refresh token; keep whichever it returned
        refresh = self.sealer.seal("refresh", {**common, "fb_rt": session.refresh_token}, ttl=REFRESH_TTL)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=access_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    # --- Access tokens and revocation ------------------------------------------------

    async def load_access_token(self, token: str) -> Optional[HKAccessToken]:
        data = self.sealer.open("access", token)
        if not data:
            return None
        return HKAccessToken(
            token=token,
            client_id=data["cid"],
            scopes=data["scopes"],
            expires_at=data["exp"],
            resource=data.get("resource"),
            subject=data["email"],
            id_token=data["idt"],
            id_token_expires_at=data["idx"],
            uid=data["uid"],
            timezone=data.get("tz"),
        )

    async def revoke_token(self, token: Any) -> None:
        # Firebase offers no client-side refresh token revocation, and access tokens can't be
        # revoked individually (they expire within an hour). Revocation must not error (RFC 7009).
        return None

    # --- Sign-in page ----------------------------------------------------------------------

    def login_routes(self) -> List[Route]:
        return [Route("/login", self.login, methods=["GET", "POST"])]

    async def login(self, request: Request) -> Response:
        if request.method == "GET":
            req = request.query_params.get("req", "")
            form: Dict[str, str] = {}
        else:
            form = {k: str(v) for k, v in (await request.form()).items()}
            req = form.get("req", "")

        pending = self.sealer.open("authreq", req)
        if not pending:
            return _page(
                "Sign-in link expired",
                "<p>This sign-in link is invalid or has expired. Go back to Claude and connect again.</p>",
                status=400,
            )
        if request.method == "GET":
            return self._login_form(req, pending)

        email = form.get("email", "").strip()
        password = form.get("password", "")
        tz = form.get("timezone", "").strip() or self.settings.default_timezone

        def retry(error: str) -> Response:
            return self._login_form(req, pending, email, error, tz)

        if not email or not password:
            return retry("Enter your email and password.")
        if not valid_timezone(tz):
            return retry("Enter a time zone name like America/Chicago or Europe/London.")
        try:
            session = await firebase_auth.sign_in(email, password)
        except firebase_auth.AuthError:
            return retry("Email or password is incorrect.")
        except firebase_auth.HuckleberryError as e:
            return retry(f"Couldn't sign in to Huckleberry: {e}")

        code = self.sealer.seal(
            "code",
            {
                "cid": pending["cid"],
                "redirect_uri": pending["redirect_uri"],
                "explicit": pending["explicit"],
                "code_challenge": pending["code_challenge"],
                "scopes": pending["scopes"],
                "resource": pending.get("resource"),
                "email": email.lower(),
                "uid": session.user_uid,
                "fb_rt": session.refresh_token,
                "tz": tz,
            },
            ttl=CODE_TTL,
        )
        target = construct_redirect_uri(pending["redirect_uri"], code=code, state=pending.get("state"))
        return RedirectResponse(target, status_code=302, headers=_SECURITY_HEADERS)

    def _login_form(
        self, req: str, pending: Dict[str, Any], email: str = "", error: str = "", tz: str = ""
    ) -> Response:
        host = urlparse(pending["redirect_uri"]).netloc or pending["redirect_uri"]
        loopback = host.split(":")[0] in ("localhost", "127.0.0.1")
        warning = (
            "<p class=warn>This request will send access to a program running on this computer "
            f"({html.escape(host)}). Only continue if you just connected Claude Code.</p>"
            if loopback
            else ""
        )
        error_html = f"<p class=error role=alert>{html.escape(error)}</p>" if error else ""
        body = f"""
<p><strong>{html.escape(pending["client_name"])}</strong> wants to read and log sleep, feeds,
diapers, and other entries in your Huckleberry account. You'll be sent back to
<strong>{html.escape(host)}</strong>.</p>
{warning}
{error_html}
<form method=post action=/login>
  <input type=hidden name=req value="{html.escape(req)}">
  <label>Huckleberry email<input name=email type=email autocomplete=username required
    value="{html.escape(email)}"></label>
  <label>Password<input name=password type=password autocomplete=current-password required></label>
  <label>Time zone<input id=tz name=timezone required autocapitalize=off spellcheck=false
    value="{html.escape(tz or self.settings.default_timezone)}"{" data-keep" if tz else ""}></label>
  <button type=submit>Sign in and allow</button>
</form>
<script>{_TZ_SCRIPT}</script>
<p class=small>Your password is sent to Huckleberry once to sign in and is never stored. This only
works for accounts that sign in with an email and password. Times you give Claude, like "10:30", are read
in this time zone unless you say otherwise.</p>"""
        return _page("Connect Huckleberry", body, status=400 if error else 200)


# Prefills the time zone field from the browser, unless the user already typed one
_TZ_SCRIPT = (
    "const f=document.getElementById('tz');"
    "if(!f.hasAttribute('data-keep')){try{f.value=Intl.DateTimeFormat().resolvedOptions().timeZone||f.value}"
    "catch(e){}}"
)
_TZ_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_TZ_SCRIPT.encode()).digest()).decode()

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    # form-action also governs the redirect after sign-in, to Claude or a Claude Code loopback port
    "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-{_TZ_SCRIPT_HASH}'; "
    "form-action 'self' https: http://localhost:* http://127.0.0.1:*; "
    "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; --fg:#1d1d1f; --bg:#f5f5f7; --card:#fff; --muted:#6e6e73; --accent:#2f7d5b; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --fg:#f5f5f7; --bg:#111; --card:#1c1c1e; --muted:#a1a1a6; --accent:#4fb385; }} }}
  body {{ margin:0; font:16px/1.5 system-ui,-apple-system,sans-serif; color:var(--fg); background:var(--bg); }}
  main {{ max-width:420px; margin:8vh auto; padding:24px; background:var(--card); border-radius:16px; }}
  h1 {{ font-size:1.4rem; margin-top:0; }}
  label {{ display:block; margin:14px 0; font-weight:600; }}
  input {{ display:block; width:100%; box-sizing:border-box; margin-top:6px; padding:12px; font:inherit;
          border:1px solid var(--muted); border-radius:10px; background:transparent; color:inherit; }}
  button {{ width:100%; padding:12px; font:inherit; font-weight:600; color:#fff; background:var(--accent);
           border:0; border-radius:10px; cursor:pointer; }}
  .error {{ color:#d70015; }} .warn {{ color:#b25000; }} .small {{ color:var(--muted); font-size:.85rem; }}
  @media (max-width:480px) {{ main {{ margin:0; border-radius:0; min-height:100vh; padding:16px; }} }}
</style></head>
<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>""",
        status_code=status,
        headers=_SECURITY_HEADERS,
    )


def info_page(settings: Settings) -> HTMLResponse:
    body = f"""<p>This is a private Model Context Protocol server for a Huckleberry account.</p>
<p>To use it, add <code>{html.escape(settings.mcp_url)}</code> as a custom connector in Claude
(Settings → Connectors → Add custom connector).</p>"""
    return _page("Huckleberry MCP server", body)
