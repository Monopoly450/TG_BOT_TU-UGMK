import os
import json
import base64
import logging
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

class SecureStore:
    def __init__(self, filename: str, password: str):
        self.filename = filename
        if not password:
            logger.warning("No password provided for SecureStore. Using default (NOT SECURE).")
            password = "default_unsafe_password"
            
        # Derive a secure key from the password
        salt = b'tg_bot_secure_salt_123'  # Fixed salt since password acts as the primary key source
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=480000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        self.fernet = Fernet(key)

    def _read_encrypted(self) -> dict:
        if not os.path.exists(self.filename):
            return {}
        try:
            with open(self.filename, 'rb') as f:
                encrypted_data = f.read()
            if not encrypted_data:
                return {}
            decrypted_data = self.fernet.decrypt(encrypted_data)
            return json.loads(decrypted_data.decode('utf-8'))
        except Exception as e:
            logger.error(f"Failed to read/decrypt secure store: {e}")
            return {}

    def _write_encrypted(self, data: dict):
        try:
            json_data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            encrypted_data = self.fernet.encrypt(json_data)
            
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.filename), exist_ok=True)
            
            with open(self.filename, 'wb') as f:
                f.write(encrypted_data)
        except Exception as e:
            logger.error(f"Failed to write/encrypt secure store: {e}")

    def save_user(self, user_id: str, profile_data: dict):
        data = self._read_encrypted()
        data[str(user_id)] = profile_data
        self._write_encrypted(data)

    def get_user(self, user_id: str) -> dict:
        data = self._read_encrypted()
        return data.get(str(user_id), {})
