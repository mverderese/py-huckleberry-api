"""The ASGI app: OAuth endpoints, the sign-in page, and the MCP endpoint at /mcp."""

from typing import Any

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.routes import build_resource_metadata_url, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route

from . import tools
from .config import Settings
from .oauth import SCOPE, StatelessOAuthProvider, info_page


class PerRequestMCP:
    """Serve each MCP request with its own stateless session manager.

    The SDK's default app starts one long-lived manager in the ASGI lifespan. Serverless
    platforms don't reliably run lifespan events, and stateless mode keeps nothing between
    requests anyway, so a fresh manager per request is both simpler and safer here.
    """

    def __init__(self, server: Any):
        self.server = server

    async def __call__(self, scope, receive, send) -> None:
        manager = StreamableHTTPSessionManager(app=self.server, json_response=True, stateless=True)
        async with manager.run():
            await manager.handle_request(scope, receive, send)


def build_app(settings: Settings) -> Starlette:
    tools.DEFAULT_TIMEZONE = settings.default_timezone
    provider = StatelessOAuthProvider(settings)
    # AuthSettings keeps a path-less issuer free of a trailing slash; issuer comparison is exact
    urls = AuthSettings(
        issuer_url=settings.base_url, resource_server_url=settings.mcp_url, validate_token_resource=False
    )
    issuer, resource = urls.issuer_url, urls.resource_server_url
    assert resource is not None  # set just above

    routes = create_auth_routes(
        provider=provider,
        issuer_url=issuer,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    routes += create_protected_resource_routes(
        resource_url=resource,
        authorization_servers=[issuer],
        scopes_supported=[SCOPE],
        resource_name="Huckleberry",
    )
    routes += provider.login_routes()
    routes += [
        Route(
            "/mcp",
            RequireAuthMiddleware(
                PerRequestMCP(tools.mcp._lowlevel_server), [SCOPE], build_resource_metadata_url(resource)
            ),
        ),
        Route("/", lambda request: info_page(settings)),
    ]

    return Starlette(
        routes=routes,
        middleware=[
            # ProviderTokenVerifier is typed for the SDK's base token classes, not subclasses
            Middleware(
                AuthenticationMiddleware,
                backend=BearerAuthBackend(ProviderTokenVerifier(provider)),  # ty: ignore[invalid-argument-type]
            ),
            Middleware(AuthContextMiddleware),
        ],
    )


def create_app() -> Starlette:
    return build_app(Settings.from_env())
