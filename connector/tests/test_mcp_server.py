"""End-to-end test of the OAuth flow and MCP tools, with Firebase and Firestore faked out.

Run with: uv run pytest  (from connector/)
"""

import base64
import hashlib
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from huckleberry_api.firebase_types import (
    FirebaseChildDocument,
    FirebaseDiaperData,
    FirebaseMedicationTypeDocument,
    FirebaseSleepDocumentData,
    FirebaseUserDocument,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from huckleberry_mcp import firebase_auth  # noqa: E402
from huckleberry_mcp.app import build_app  # noqa: E402
from huckleberry_mcp.config import Settings  # noqa: E402

BASE = "https://hk.example.com"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
EMAIL = "parent@example.com"
PASSWORD = "correct horse"
UID = "user-1"


class FakeAPI:
    """Stands in for HuckleberryAPI, recording writes."""

    def __init__(self, fake: "FakeFirebase", timezone: str):
        self.fake = fake
        self.timezone = timezone

    def __getattr__(self, name):
        async def record(*args, **kwargs):
            self.fake.calls.append((name, *args))

        return record

    async def get_user(self):
        return FirebaseUserDocument.model_validate({"childList": [{"cid": c} for c in self.fake.children]})

    async def get_child(self, cid):
        return FirebaseChildDocument.model_validate({"childsName": self.fake.children[cid]})

    async def get_sleep(self, cid):
        return FirebaseSleepDocumentData.model_validate(self.fake.sleep)

    async def start_sleep(self, cid):
        self.fake.calls.append(("start_sleep", cid))
        self.fake.sleep = {"timer": {"active": True, "paused": False, "timerStartTime": 1_760_000_000_000, "uuid": "u"}}

    async def list_diaper_intervals(self, cid, start, end):
        return [
            FirebaseDiaperData(mode="pee", start=1_760_000_000, offset=240),
            FirebaseDiaperData(mode="both", start=1_760_003_600, offset=240),
        ]

    async def log_diaper(self, cid, **kwargs):
        self.fake.calls.append(("log_diaper", cid, kwargs))

    async def log_sleep(self, cid, **kwargs):
        self.fake.calls.append(("log_sleep", cid, kwargs))

    async def list_medicine_types(self, cid):
        return [
            FirebaseMedicationTypeDocument.model_validate(
                {"_id": "m1", "active": True, "mode": "medication", "name": "Vitamin D"}
            )
        ]

    async def log_medicine(self, cid, **kwargs):
        self.fake.calls.append(("log_medicine", cid, kwargs))


class FakeFirebase:
    def __init__(self):
        self.children = {"c1": "Chloe"}
        self.sleep: dict = {}
        self.calls: list = []
        self.api_args: list = []
        self.refresh_tokens = {"fb-refresh"}

    async def sign_in(self, email, password):
        if (email, password) != (EMAIL, PASSWORD):
            raise firebase_auth.AuthError("INVALID_LOGIN_CREDENTIALS")
        return firebase_auth.Session("id-1", "fb-refresh", UID, time.time() + 3600)

    async def refresh(self, refresh_token, uid):
        if refresh_token not in self.refresh_tokens:
            raise firebase_auth.AuthError("INVALID_REFRESH_TOKEN")
        assert uid == UID
        return firebase_auth.Session("id-2", "fb-refresh", uid, time.time() + 3600)

    @asynccontextmanager
    async def api_client(self, email, id_token, user_uid, expires_at, timezone):
        self.api_args.append((email, id_token, user_uid, timezone))
        yield FakeAPI(self, timezone)


@pytest.fixture
def fake(monkeypatch):
    fake = FakeFirebase()
    monkeypatch.setattr(firebase_auth, "sign_in", fake.sign_in)
    monkeypatch.setattr(firebase_auth, "refresh", fake.refresh)
    monkeypatch.setattr(firebase_auth, "api_client", fake.api_client)
    return fake


@pytest.fixture
def client(fake):
    settings = Settings(
        base_url=BASE,
        token_secrets=[Fernet.generate_key().decode()],
        default_timezone="America/New_York",
        allow_loopback_redirects=True,
    )
    return TestClient(build_app(settings), base_url=BASE)


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(client, redirect=CALLBACK):
    return client.post(
        "/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [redirect],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )


def sign_in(client, email=EMAIL, password=PASSWORD, tz=None):
    """Run register -> authorize -> login; return (client_id, verifier, login response)."""
    client_id = register(client).json()["client_id"]
    verifier, challenge = pkce()
    r = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "state": "st8",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "huckleberry",
            "resource": f"{BASE}/mcp",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    login_url = urlparse(r.headers["location"])
    assert login_url.path == "/login"
    req = parse_qs(login_url.query)["req"][0]

    form = client.get(f"/login?req={req}")
    assert form.status_code == 200 and "claude.ai" in form.text and "Huckleberry" in form.text

    data = {"req": req, "email": email, "password": password}
    if tz is not None:
        data["timezone"] = tz
    r = client.post("/login", data=data, follow_redirects=False)
    return client_id, verifier, r


def get_tokens(client, tz=None):
    client_id, verifier, r = sign_in(client, tz=tz)
    assert r.status_code == 302, r.text
    back = urlparse(r.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == CALLBACK
    query = parse_qs(back.query)
    assert query["state"] == ["st8"]
    r = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    return client_id, r.json()


def mcp_call(client, token, method, params=None, id_=1):
    r = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        },
        json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
    )
    assert r.status_code == 200, r.text
    return r.json()


def call_tool(client, token, name, arguments):
    return mcp_call(client, token, "tools/call", {"name": name, "arguments": arguments})["result"]


def result_of(result):
    assert not result.get("isError"), result
    structured = result["structuredContent"]
    return structured.get("result", structured)


# --- Discovery and sign-in ---------------------------------------------------------------


def test_unauthenticated_mcp_points_to_metadata(client):
    r = client.post("/mcp", json={})
    assert r.status_code == 401
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]

    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == f"{BASE}/mcp"
    assert prm["authorization_servers"] == [BASE]

    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["issuer"] == BASE
    assert asm["registration_endpoint"] == f"{BASE}/register"
    assert "S256" in asm["code_challenge_methods_supported"]


def test_registration_rejects_foreign_redirects(client):
    assert register(client, "https://evil.example.com/cb").status_code == 400
    assert register(client, "http://localhost:4567/callback").status_code == 201


def test_wrong_password(client):
    _, _, r = sign_in(client, password="nope")
    assert r.status_code == 400 and "Email or password is incorrect." in r.text


def test_unknown_time_zone_is_rejected(client):
    _, _, r = sign_in(client, tz="Mars/Olympus_Mons")
    assert r.status_code == 400 and 'value="Mars/Olympus_Mons" data-keep' in r.text
    script = r.text.split("<script>")[1].split("</script>")[0]
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in r.headers["content-security-policy"]


def test_code_requires_pkce_verifier(client):
    client_id, _, r = sign_in(client)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "client_id": client_id,
            "code_verifier": "x" * 50,
        },
    )
    assert r.status_code == 400


def test_tampered_or_wrong_type_tokens_rejected(client):
    _, tokens = get_tokens(client)
    access = tokens["access_token"]
    assert access.startswith("hka_") and tokens["refresh_token"].startswith("hkr_")
    tampered = access[:-4] + ("AAAA" if not access.endswith("AAAA") else "BBBB")
    for bad in (tampered, tokens["refresh_token"], "hka_garbage"):
        r = client.post("/mcp", headers={"Authorization": f"Bearer {bad}"}, json={})
        assert r.status_code == 401


def test_access_token_expires_before_id_token(client):
    _, tokens = get_tokens(client)
    # The ID token lives an hour; the access token must end 10 minutes before it
    assert 2900 <= tokens["expires_in"] <= 3000


def test_refresh_and_expired_sign_in(client, fake):
    client_id, tokens = get_tokens(client)
    r = client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] != tokens["access_token"]

    fake.refresh_tokens.clear()  # Firebase no longer accepts it (password changed, account disabled, ...)
    r = client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id},
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"

    # Revocation is accepted even though Firebase can't revoke from the client side
    r = client.post("/revoke", data={"token": tokens["refresh_token"], "client_id": client_id, "client_secret": ""})
    assert r.status_code == 200


# --- Tools ---------------------------------------------------------------------------------


def test_tools_end_to_end(client, fake):
    _, tokens = get_tokens(client, tz="America/Chicago")
    token = tokens["access_token"]

    init = mcp_call(
        client,
        token,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    )
    assert init["result"]["serverInfo"]["name"] == "Huckleberry"

    tools = {t["name"]: t for t in mcp_call(client, token, "tools/list")["result"]["tools"]}
    assert {"get_current_status", "get_entries", "sleep_timer", "log_diaper", "cancel_timer"} <= set(tools)
    assert tools["get_entries"]["annotations"]["readOnlyHint"] is True
    assert tools["cancel_timer"]["annotations"]["destructiveHint"] is True

    assert result_of(call_tool(client, token, "list_children", {})) == [
        {"id": "c1", "name": "Chloe", "birthdate": None}
    ]
    # Tools use the ID token minted at sign-in, for this user, in the zone chosen on the sign-in page
    assert fake.api_args[-1] == (EMAIL, "id-2", UID, "America/Chicago")

    started = result_of(call_tool(client, token, "sleep_timer", {"action": "start"}))
    assert started["sleep_timer"]["running"] is True and started["child"] == "Chloe"
    assert fake.calls == [("start_sleep", "c1")]

    call_tool(client, token, "log_diaper", {"mode": "both", "time": "2026-10-14T03:10:00", "color": "yellow"})
    name, cid, kwargs = fake.calls[-1]
    assert (name, cid, kwargs["mode"], kwargs["color"]) == ("log_diaper", "c1", "both", "yellow")
    assert kwargs["start_time"].isoformat() == "2026-10-14T03:10:00-05:00"

    bad = call_tool(client, token, "log_diaper", {"mode": "sparkly"})
    assert bad["isError"] and len(fake.calls) == 2

    entries = result_of(call_tool(client, token, "get_entries", {"tracker": "diaper"}))
    assert entries["count"] == 2
    assert [e["mode"] for e in entries["entries"]] == ["both", "pee"]
    assert entries["entries"][0]["start_local"].endswith("CDT")


def test_log_sleep_and_medicine(client, fake):
    _, tokens = get_tokens(client)
    token = tokens["access_token"]

    logged = result_of(
        call_tool(
            client,
            token,
            "log_sleep",
            {
                "start_time": "2026-10-19T13:00:00-04:00",
                "end_time": "2026-10-19T14:30:00-04:00",
                "locations": ["stroller"],
                "notes": "walk",
            },
        )
    )
    assert logged["minutes"] == 90
    details = fake.calls[-1][2]["details"]
    assert details.sleepLocations.stroller is True and details.notes == "walk"

    backwards = call_tool(
        client, token, "log_sleep", {"start_time": "2026-10-19T14:30:00-04:00", "end_time": "2026-10-19T13:00:00-04:00"}
    )
    assert backwards["isError"]

    result_of(call_tool(client, token, "log_medicine", {"medicine": "vitamin d", "amount": 1, "units": "ml"}))
    assert fake.calls[-1][2]["medicine_type"].id_ == "m1"
    unknown = call_tool(client, token, "log_medicine", {"medicine": "Tylenol"})
    assert unknown["isError"] and "create_medicine" in unknown["content"][0]["text"]


def test_several_children_need_a_name(client, fake):
    fake.children = {"c1": "Chloe", "c2": "Max"}
    _, tokens = get_tokens(client)
    token = tokens["access_token"]

    ambiguous = call_tool(client, token, "sleep_timer", {"action": "start"})
    assert ambiguous["isError"] and "Chloe, Max" in ambiguous["content"][0]["text"]
    result_of(call_tool(client, token, "sleep_timer", {"action": "start", "child": "max"}))
    assert fake.calls == [("start_sleep", "c2")]


# --- Firebase error translation ------------------------------------------------------------


def _response_error(status, message):
    return aiohttp.ClientResponseError(None, (), status=status, message=message)  # ty: ignore[invalid-argument-type]


def test_firebase_errors_are_translated():
    assert isinstance(
        firebase_auth._translate(_response_error(400, "INVALID_LOGIN_CREDENTIALS")), firebase_auth.AuthError
    )
    too_many = firebase_auth._translate(_response_error(400, "TOO_MANY_ATTEMPTS_TRY_LATER"))
    assert not isinstance(too_many, firebase_auth.AuthError) and "Too many" in str(too_many)
    assert not isinstance(firebase_auth._translate(_response_error(503, "down")), firebase_auth.AuthError)


async def _enter_expired_client():
    async with firebase_auth.api_client(EMAIL, "id", UID, time.time() + 60, "UTC"):
        pass


def test_api_client_refuses_nearly_expired_id_token():
    import asyncio

    with pytest.raises(firebase_auth.AuthError):
        asyncio.run(_enter_expired_client())


async def _real_client_uses_token_as_is():
    async with firebase_auth.api_client(EMAIL, "id-x", UID, time.time() + 3000, "Europe/London") as api:
        await api._ensure_authenticated()  # would raise without a password or refresh token
        return api.id_token, api.user_uid, str(api._timezone)


def test_api_client_uses_the_sealed_id_token():
    import asyncio

    assert asyncio.run(_real_client_uses_token_as_is()) == ("id-x", UID, "Europe/London")
