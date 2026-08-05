import pytest
from cryptography.fernet import Fernet

from codex_model_switcher.crypto import CryptoError, FernetCipher, InvalidSecretKeyError


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


def test_fernet_cipher_wraps_invalid_token_without_exposing_token() -> None:
    cipher = FernetCipher(FakeSecretKeyProvider())
    invalid_token = b"not-a-valid-token"

    with pytest.raises(CryptoError, match="could not be decrypted") as error:
        cipher.decrypt_text(invalid_token)

    assert invalid_token.decode() not in str(error.value)
    assert error.value.__cause__ is None


def test_fernet_cipher_wraps_invalid_utf8_without_exposing_plaintext() -> None:
    provider = FakeSecretKeyProvider()
    cipher = FernetCipher(provider)
    invalid_utf8 = b"\xff\xfe"
    token = Fernet(provider.key).encrypt(invalid_utf8)

    with pytest.raises(CryptoError, match="could not be decrypted") as error:
        cipher.decrypt_text(token)

    assert "invalid" not in str(error.value)
    assert error.value.__cause__ is None
