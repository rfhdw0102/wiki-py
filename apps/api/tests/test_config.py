import pytest
from pydantic import ValidationError
from wiki_agent.config import Settings


def test_legacy_bootstrap_admin_setting_is_ignored() -> None:
    settings = Settings(_env_file=None, bootstrap_admin_email="admin@example.com")

    assert not hasattr(settings, "bootstrap_admin_email")


def test_production_rejects_development_secret() -> None:
    with pytest.raises(ValidationError, match="unique WIKI_AGENT_SECRET_KEY"):
        Settings(_env_file=None, env="production", encryption_key="configured")


def test_smtp_credentials_must_be_complete() -> None:
    with pytest.raises(ValidationError, match="must be configured together"):
        Settings(_env_file=None, smtp_host="smtp.qq.com")


def test_production_requires_smtp_configuration() -> None:
    with pytest.raises(ValidationError, match="QQ SMTP"):
        Settings(
            _env_file=None,
            env="production",
            secret_key="a-unique-production-secret-with-32-characters",
            encryption_key="configured",
        )
