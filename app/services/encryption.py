"""Token encryption service using Fernet symmetric encryption.

This service encrypts Google refresh tokens before storing them in the database.
Uses Fernet (symmetric encryption) with a key derived from the app's SECRET_KEY.
"""
import base64
import hashlib
from cryptography.fernet import Fernet


class EncryptionService:
    """Service for encrypting and decrypting sensitive data."""

    def __init__(self, secret_key: str):
        """Initialize encryption service with a secret key.

        Args:
            secret_key: Application secret key (must be 32+ characters)

        The secret key is hashed to produce a 32-byte Fernet key.
        """
        if len(secret_key) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters for secure encryption")

        # Derive 32-byte key from SECRET_KEY using SHA-256
        key_bytes = hashlib.sha256(secret_key.encode()).digest()
        fernet_key = base64.urlsafe_b64encode(key_bytes)
        self.cipher = Fernet(fernet_key)

    def encrypt(self, plaintext: str) -> str:
        """Encrypt plaintext and return base64-encoded ciphertext.

        Args:
            plaintext: The string to encrypt

        Returns:
            Base64-encoded encrypted string

        Example:
            >>> service = EncryptionService("my-super-secret-key-32-chars-minimum")
            >>> encrypted = service.encrypt("refresh_token_abc123")
            >>> encrypted
            'gAAAAABl...'
        """
        if not plaintext:
            raise ValueError("Cannot encrypt empty string")

        return self.cipher.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt base64-encoded ciphertext and return plaintext.

        Args:
            ciphertext: The encrypted string to decrypt

        Returns:
            Decrypted plaintext string

        Raises:
            cryptography.fernet.InvalidToken: If ciphertext is invalid or key is wrong

        Example:
            >>> service = EncryptionService("my-super-secret-key-32-chars-minimum")
            >>> decrypted = service.decrypt("gAAAAABl...")
            >>> decrypted
            'refresh_token_abc123'
        """
        if not ciphertext:
            raise ValueError("Cannot decrypt empty string")

        return self.cipher.decrypt(ciphertext.encode()).decode()


# Global instance (initialized in main.py after loading config)
_encryption_service: EncryptionService | None = None


def init_encryption_service(secret_key: str) -> None:
    """Initialize the global encryption service instance.

    This should be called once at app startup in main.py

    Args:
        secret_key: Application secret key from settings
    """
    global _encryption_service
    _encryption_service = EncryptionService(secret_key)


def get_encryption_service() -> EncryptionService:
    """Get the global encryption service instance.

    Returns:
        The initialized encryption service

    Raises:
        RuntimeError: If encryption service hasn't been initialized
    """
    if _encryption_service is None:
        raise RuntimeError(
            "Encryption service not initialized. "
            "Call init_encryption_service() in main.py first."
        )
    return _encryption_service
