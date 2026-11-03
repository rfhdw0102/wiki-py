from __future__ import annotations

from email.message import EmailMessage

import aiosmtplib

from wiki_agent.auth.store import AuthPurpose
from wiki_agent.config import Settings


class EmailDeliveryError(RuntimeError):
    pass


class EmailSender:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send_verification_code(
        self,
        recipient: str,
        code: str,
        purpose: AuthPurpose,
    ) -> None:
        settings = self._settings
        if (
            settings.smtp_host is None
            or settings.smtp_username is None
            or settings.smtp_auth_code is None
        ):
            raise EmailDeliveryError("SMTP is not configured")

        action = {
            AuthPurpose.PASSWORD_RESET: "重置密码",
            AuthPurpose.EMAIL_BIND: "绑定邮箱",
            AuthPurpose.LOGIN: "登录",
        }[purpose]
        message = EmailMessage()
        message["From"] = f"{settings.email_from_name} <{settings.smtp_username}>"
        message["To"] = recipient
        message["Subject"] = f"{settings.email_from_name} {action}验证码"
        message.set_content(
            f"你的{action}验证码是：{code}\n\n"
            f"验证码将在 {settings.email_code_ttl_seconds // 60} 分钟后失效。"
            "如果不是你本人操作，请忽略这封邮件。"
        )
        message.add_alternative(
            "<p>你的"
            f"{action}验证码是：</p><p style=\"font-size: 28px; "
            "font-weight: 700; letter-spacing: 6px;\">"
            f"{code}</p><p>验证码将在 "
            f"{settings.email_code_ttl_seconds // 60} 分钟后失效。"
            "如果不是你本人操作，请忽略这封邮件。</p>",
            subtype="html",
        )

        try:
            await aiosmtplib.send(
                message,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username,
                password=settings.smtp_auth_code.get_secret_value(),
                use_tls=settings.smtp_use_tls,
                timeout=15,
            )
        except (aiosmtplib.SMTPException, OSError, TimeoutError) as exc:
            raise EmailDeliveryError("Verification email delivery failed") from exc
