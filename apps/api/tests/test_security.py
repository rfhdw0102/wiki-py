from cryptography.fernet import Fernet
from wiki_agent.security import SecretBox


def test_secret_box_round_trip() -> None:
    box = SecretBox(Fernet.generate_key().decode())
    encrypted = box.encrypt("sk-secret")
    assert encrypted != "sk-secret"
    assert box.decrypt(encrypted) == "sk-secret"
