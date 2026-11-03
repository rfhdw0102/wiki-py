from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import cast

from redis.asyncio import Redis

from wiki_agent.config import Settings


class AuthPurpose(StrEnum):
    LOGIN = "login"
    PASSWORD_RESET = "password_reset"
    EMAIL_BIND = "email_bind"


@dataclass(frozen=True)
class CaptchaChallengeState:
    target_x: int
    purpose: AuthPurpose


class VerificationStore:
    def __init__(self, settings: Settings, redis: Redis | None = None) -> None:
        self._redis = redis or Redis.from_url(settings.redis_url, decode_responses=True)
        self._secret = settings.secret_key.encode()
        self._captcha_challenge_ttl = settings.captcha_challenge_ttl_seconds
        self._captcha_token_ttl = settings.captcha_token_ttl_seconds
        self._email_code_ttl = settings.email_code_ttl_seconds
        self._email_code_cooldown = settings.email_code_cooldown_seconds
        self._email_code_max_attempts = settings.email_code_max_attempts

    async def close(self) -> None:
        await self._redis.aclose()

    async def save_captcha_challenge(
        self,
        state: CaptchaChallengeState,
    ) -> tuple[str, int]:
        challenge_id = secrets.token_urlsafe(24)
        await self._redis.setex(
            self._captcha_challenge_key(challenge_id),
            self._captcha_challenge_ttl,
            json.dumps(asdict(state)),
        )
        return challenge_id, self._captcha_challenge_ttl

    async def consume_captcha_challenge(
        self,
        challenge_id: str,
    ) -> CaptchaChallengeState | None:
        raw = await self._redis.getdel(self._captcha_challenge_key(challenge_id))
        if raw is None:
            return None
        data = json.loads(raw)
        return CaptchaChallengeState(
            target_x=int(data["target_x"]),
            purpose=AuthPurpose(data["purpose"]),
        )

    async def issue_captcha_token(self, purpose: AuthPurpose) -> tuple[str, int]:
        token = secrets.token_urlsafe(32)
        await self._redis.setex(
            self._captcha_token_key(token),
            self._captcha_token_ttl,
            purpose.value,
        )
        return token, self._captcha_token_ttl

    async def consume_captcha_token(self, token: str, purpose: AuthPurpose) -> bool:
        stored_purpose = cast(
            str | None,
            await self._redis.getdel(self._captcha_token_key(token)),
        )
        return stored_purpose == purpose.value

    async def acquire_email_cooldown(self, email: str, purpose: AuthPurpose) -> bool:
        result = await self._redis.set(
            self._email_cooldown_key(email, purpose),
            "1",
            ex=self._email_code_cooldown,
            nx=True,
        )
        return bool(result)

    async def release_email_cooldown(self, email: str, purpose: AuthPurpose) -> None:
        await self._redis.delete(self._email_cooldown_key(email, purpose))

    async def save_email_code(
        self,
        email: str,
        purpose: AuthPurpose,
        code: str,
    ) -> None:
        payload = {
            "digest": self._code_digest(email, purpose, code),
            "attempts": 0,
            "expires_at": int(time.time()) + self._email_code_ttl,
        }
        await self._redis.setex(
            self._email_code_key(email, purpose),
            self._email_code_ttl,
            json.dumps(payload),
        )

    async def consume_email_code(
        self,
        email: str,
        purpose: AuthPurpose,
        code: str,
    ) -> bool:
        key = self._email_code_key(email, purpose)
        raw = await self._redis.getdel(key)
        if raw is None:
            return False
        payload = json.loads(raw)
        actual = self._code_digest(email, purpose, code)
        if hmac.compare_digest(str(payload["digest"]), actual):
            return True

        attempts = int(payload["attempts"]) + 1
        remaining = int(payload["expires_at"]) - int(time.time())
        if attempts < self._email_code_max_attempts and remaining > 0:
            payload["attempts"] = attempts
            await self._redis.setex(key, remaining, json.dumps(payload))
        return False

    async def check_rate_limit(
        self,
        action: str,
        subject: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> bool:
        key = self._rate_limit_key(action, subject)
        count = cast(int, await self._redis.incr(key))
        if count == 1:
            await self._redis.expire(key, window_seconds)
        return count <= limit

    def _code_digest(self, email: str, purpose: AuthPurpose, code: str) -> str:
        message = f"{purpose.value}:{email}:{code}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    @staticmethod
    def _captcha_challenge_key(challenge_id: str) -> str:
        return f"auth:captcha:challenge:{challenge_id}"

    @staticmethod
    def _captcha_token_key(token: str) -> str:
        digest = hashlib.sha256(token.encode()).hexdigest()
        return f"auth:captcha:token:{digest}"

    @classmethod
    def _email_code_key(cls, email: str, purpose: AuthPurpose) -> str:
        return f"auth:email-code:{purpose.value}:{cls._subject_digest(email)}"

    @classmethod
    def _email_cooldown_key(cls, email: str, purpose: AuthPurpose) -> str:
        return f"auth:email-cooldown:{purpose.value}:{cls._subject_digest(email)}"

    @classmethod
    def _rate_limit_key(cls, action: str, subject: str) -> str:
        return f"auth:rate:{action}:{cls._subject_digest(subject)}"

    @staticmethod
    def _subject_digest(subject: str) -> str:
        return hashlib.sha256(subject.encode()).hexdigest()
