import pytest
from pydantic import ValidationError
from wiki_agent.config import Settings


def test_bootstrap_credentials_must_be_complete() -> None:
    with pytest.raises(ValidationError, match="configured together"):
        Settings(_env_file=None, bootstrap_admin_email="admin@example.com")


def test_production_rejects_development_secret() -> None:
    with pytest.raises(ValidationError, match="unique WIKI_AGENT_SECRET_KEY"):
        Settings(_env_file=None, env="production", encryption_key="configured")
