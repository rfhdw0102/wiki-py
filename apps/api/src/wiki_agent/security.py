import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from wiki_agent.config import Settings

password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def create_token(
    subject: str,
    token_type: str,
    settings: Settings,
    expires_delta: timedelta,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(claims, settings.secret_key, algorithm="HS256")


def decode_token(token: str, expected_type: str, settings: Settings) -> str:
    payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    subject = payload.get("sub")
    if payload.get("type") != expected_type or not isinstance(subject, str):
        raise jwt.InvalidTokenError("unexpected token type")
    return subject


def create_refresh_token(session_id: str) -> tuple[str, str]:
    secret = secrets.token_urlsafe(48)
    token = f"{session_id}.{secret}"
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def refresh_session_id(token: str) -> str:
    session_id, separator, secret = token.partition(".")
    if not separator or not session_id or not secret:
        raise ValueError("malformed refresh token")
    return session_id


class SecretEncryptionError(RuntimeError):
    pass


class SecretBox:
    def __init__(self, key: str | None) -> None:
        if not key:
            raise SecretEncryptionError(
                "WIKI_AGENT_ENCRYPTION_KEY is required before storing model credentials"
            )
        try:
            self._fernet = Fernet(key.encode())
        except ValueError as exc:
            raise SecretEncryptionError(
                "WIKI_AGENT_ENCRYPTION_KEY must be a valid Fernet key"
            ) from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise SecretEncryptionError("stored credential cannot be decrypted") from exc
