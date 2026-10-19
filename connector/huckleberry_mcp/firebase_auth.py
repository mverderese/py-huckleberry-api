"""Huckleberry (Firebase) sign-in, token refresh, and per-request API clients.

Sign-in and refresh go through HuckleberryAPI itself, so the connector uses the same
endpoints and payloads as the library. The server keeps nothing: the Firebase refresh
token and user id travel inside the sealed OAuth tokens (see oauth.py).
"""

import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiohttp

from huckleberry_api import HuckleberryAPI


class HuckleberryError(Exception):
    """Huckleberry or Firebase couldn't be reached, or answered unexpectedly."""


class AuthError(HuckleberryError):
    """Firebase rejected the email/password or the refresh token."""


@dataclass(frozen=True)
class Session:
    id_token: str
    refresh_token: str
    user_uid: str
    expires_at: float  # epoch seconds when id_token expires


def _translate(err: Exception) -> HuckleberryError:
    if isinstance(err, aiohttp.ClientResponseError) and err.status in (400, 401, 403):
        # Firebase answers 400 for wrong passwords, revoked/expired refresh tokens, and disabled users
        if "TOO_MANY_ATTEMPTS" in (err.message or ""):
            return HuckleberryError("Too many sign-in attempts; wait a few minutes and try again")
        return AuthError(err.message)
    if isinstance(err, aiohttp.ClientResponseError):
        return HuckleberryError(err.message)
    return HuckleberryError(str(err) or type(err).__name__)


async def sign_in(email: str, password: str) -> Session:
    async with aiohttp.ClientSession() as websession:
        api = HuckleberryAPI(email=email, password=password, timezone="UTC", websession=websession)
        try:
            await api.authenticate()
        except (aiohttp.ClientError, TimeoutError, KeyError, ValueError) as err:
            raise _translate(err) from None
    return _session(api, api.user_uid)


async def refresh(refresh_token: str, user_uid: str) -> Session:
    async with aiohttp.ClientSession() as websession:
        api = HuckleberryAPI(email="", password="", timezone="UTC", websession=websession)
        api.refresh_token = refresh_token
        try:
            await api.refresh_session_token()
        except (aiohttp.ClientError, TimeoutError, KeyError, ValueError) as err:
            raise _translate(err) from None
    return _session(api, user_uid)


def _session(api: HuckleberryAPI, user_uid: str | None) -> Session:
    if not (api.id_token and api.refresh_token and user_uid and api.token_expires_at):
        raise HuckleberryError("Huckleberry sign-in returned an incomplete response")
    return Session(api.id_token, api.refresh_token, user_uid, api.token_expires_at)


@asynccontextmanager
async def api_client(
    email: str, id_token: str, user_uid: str, expires_at: float, timezone: str
) -> AsyncGenerator[HuckleberryAPI]:
    """A HuckleberryAPI that uses an existing ID token instead of signing in.

    It has no password or refresh token, so it never re-authenticates: access tokens expire
    well before the ID token they carry (see oauth.py), and Claude refreshes them first.
    """
    if expires_at - 300 <= time.time():
        raise AuthError("Huckleberry sign-in expired")
    async with aiohttp.ClientSession() as websession:
        api = HuckleberryAPI(email=email, password="", timezone=timezone, websession=websession)
        api.id_token = id_token
        api.user_uid = user_uid
        api.token_expires_at = expires_at
        yield api
