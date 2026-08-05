import pytest
from cryptography.fernet import Fernet

from codex_model_switcher.crypto import FernetCipher, InvalidSecretKeyError


class FakeSecretKeyProvider:
    def __init__(self) -> None:
        self.key = Fernet.generate_key()
        self.calls = 0

    def get_key(self) -> bytes:
        self.calls += 1
        return self.key


def test_fernet_cipher_uses_injected_provider_and_round_trips_text() -> None:
    provider = FakeSecretKeyProvider()
    cipher = FernetCipher(provider)

    encrypted = cipher.encrypt_text("仅用于继续协议一轮的片段")

    assert provider.calls == 1
    assert encrypted != "仅用于继续协议一轮的片段"
    assert "继续协议".encode("utf-8") not in encrypted
    assert cipher.decrypt_text(encrypted) == "仅用于继续协议一轮的片段"


def test_fernet_cipher_rejects_invalid_injected_key() -> None:
    class InvalidProvider:
        def get_key(self) -> bytes:
            return b"not-a-fernet-key"

    with pytest.raises(InvalidSecretKeyError):
        FernetCipher(InvalidProvider())
