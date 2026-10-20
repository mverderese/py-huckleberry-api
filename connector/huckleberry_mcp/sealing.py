"""Encrypted, self-contained tokens, so the server needs no database.

Every OAuth artifact (registered client, pending authorization request, authorization
code, access token, refresh token) is a JSON payload encrypted and authenticated with
Fernet under TOKEN_SECRET. The prefix and the "typ" field keep one kind from being
accepted as another. Rotating TOKEN_SECRET invalidates everything at once.
"""

import hashlib
import json
import time
from typing import Any, Dict, List, Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

PREFIXES = {
    "client": "hkc_",
    "authreq": "hkq_",
    "code": "hkk_",
    "access": "hka_",
    "refresh": "hkr_",
}


class Sealer:
    def __init__(self, secrets: List[str]):
        self._fernet = MultiFernet([Fernet(s.encode()) for s in secrets])

    def seal(self, typ: str, payload: Dict[str, Any], ttl: Optional[int]) -> str:
        data = {**payload, "typ": typ}
        if ttl is not None:
            data["exp"] = int(time.time()) + ttl
        return PREFIXES[typ] + self._fernet.encrypt(json.dumps(data, separators=(",", ":")).encode()).decode()

    def open(self, typ: str, token: Optional[str]) -> Optional[Dict[str, Any]]:
        """The payload, or None if the token is not a valid, unexpired token of this type."""
        prefix = PREFIXES[typ]
        if not token or not token.startswith(prefix):
            return None
        try:
            data = json.loads(self._fernet.decrypt(token[len(prefix) :].encode()))
        except InvalidToken, ValueError:
            return None
        if not isinstance(data, dict) or data.get("typ") != typ:
            return None
        if "exp" in data and data["exp"] < time.time():
            return None
        return data


def fingerprint(value: str) -> str:
    """A short stable id for a long value (used to bind codes and tokens to a client)."""
    return hashlib.sha256(value.encode()).hexdigest()[:32]
