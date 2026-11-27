"""Who is calling: a backend service with an API key, or a learner's browser with a token.

- Services (Talkwise backend, Pipecat voice bot) send ``X-API-Key``. They are trusted to act for
  any learner, so they name the ``user_id`` themselves.
- Browsers send ``Authorization: Bearer <token>``: a short-lived HS256 JWT minted by the Talkwise
  backend with the shared secret. Claims: ``sub`` (the learner's user id), ``aud`` ("ea-llm"),
  ``iat``, ``exp`` and optionally ``sid`` (limits the token to one session). The service only
  checks signature, audience and expiry; it never calls Talkwise back.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Literal

import jwt

from ea_llm.api.errors import ApiError
from ea_llm.api.settings import ApiSettings
from ea_llm.models import SessionSnapshot

TOKEN_ALGORITHM = "HS256"
CLOCK_LEEWAY_S = 10
API_KEY_HEADER = "X-API-Key"


@dataclass(frozen=True)
class Caller:
    """The authenticated party of a request."""

    kind: Literal["service", "learner"]
    name: str  # "talkwise", "pipecat", "dev" for services; "learner" for tokens
    user_id: str | None = None  # learners only
    session_id: str | None = None  # learners only: the token is limited to this session

    def can_access(self, snapshot: SessionSnapshot) -> bool:
        """Services reach every session; a learner only their own (and the token's one)."""
        if self.kind == "service":
            return True
        if self.session_id is not None and self.session_id != snapshot.session_id:
            return False
        return self.user_id == snapshot.user_id


DEV_CALLER = Caller(kind="service", name="dev")


class Authenticator:
    """Checks API keys and learner tokens against the configured secrets."""

    def __init__(self, settings: ApiSettings) -> None:
        self._settings = settings
        self._keys = [(caller, key.encode()) for caller, key in settings.api_keys.items()]

    def authenticate(self, api_key: str | None, bearer: str | None) -> Caller:
        """The caller behind the credentials; ApiError 401 when they are missing or invalid."""
        if self._settings.auth_disabled:
            return DEV_CALLER
        if api_key:
            return self._service(api_key)
        if bearer:
            return self._learner(bearer)
        raise _unauthorized(f"send an {API_KEY_HEADER} header or a bearer token")

    def _service(self, api_key: str) -> Caller:
        given = api_key.encode()
        match = None
        for caller, key in self._keys:  # compare with every key, in constant time each
            if hmac.compare_digest(given, key):
                match = caller
        if match is None:
            raise _unauthorized("invalid API key")
        return Caller(kind="service", name=match)

    def _learner(self, token: str) -> Caller:
        secret = self._settings.token_secret
        if not secret:
            raise _unauthorized("learner tokens are not accepted by this deployment")
        try:
            claims = jwt.decode(
                token,
                secret,
                algorithms=[TOKEN_ALGORITHM],
                audience=self._settings.token_audience,
                leeway=CLOCK_LEEWAY_S,
                options={"require": ["sub", "aud", "iat", "exp"]},
            )
        except jwt.ExpiredSignatureError:
            raise _unauthorized("token expired") from None
        except jwt.PyJWTError:
            raise _unauthorized("invalid token") from None
        if claims["exp"] - claims["iat"] > self._settings.max_token_lifetime_s:
            raise _unauthorized("token lifetime is too long")
        session_id = claims.get("sid")
        if session_id is not None and not isinstance(session_id, str):
            raise _unauthorized("invalid token")
        return Caller(kind="learner", name="learner", user_id=claims["sub"], session_id=session_id)


def _unauthorized(message: str) -> ApiError:
    return ApiError(401, "unauthorized", message, headers={"WWW-Authenticate": "Bearer"})
