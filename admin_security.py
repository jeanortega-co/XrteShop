import base64
import hashlib
import os
import secrets

import bcrypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PASSWORD_HASH_PREFIX = "bcrypt-sha256$"


def _password_digest(password):
    return hashlib.sha256(password.encode("utf-8")).digest()


def hash_password(password):
    encoded = bcrypt.hashpw(_password_digest(password), bcrypt.gensalt(rounds=12))
    return PASSWORD_HASH_PREFIX + encoded.decode("ascii")


def verify_password(password_hash, password):
    try:
        if password_hash.startswith(PASSWORD_HASH_PREFIX):
            encoded = password_hash[len(PASSWORD_HASH_PREFIX):].encode("ascii")
            return bcrypt.checkpw(_password_digest(password), encoded)
        if password_hash.startswith(("$2a$", "$2b$", "$2y$")):
            return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return False
    return False


def validate_password(password, email):
    if len(password) < 8:
        return "La contraseña debe tener al menos 8 caracteres."
    if len(password.encode("utf-8")) > 4096:
        return "La contraseña supera el límite seguro permitido."
    normalized = password.strip().casefold()
    normalized_email = email.strip().casefold()
    if normalized == normalized_email:
        return "La contraseña no puede ser igual al correo."
    common_passwords = {
        "12345678", "123456789", "1234567890", "abcdefgh", "admin123",
        "iloveyou", "letmein", "password", "password1", "password123",
        "qwerty", "qwerty123", "qwertyuiop", "welcome1", "welcome123",
        "contraseña", "contrasena", "colombia", "colombia123", "colombia2020",
        "adminadmin", "abc12345", "abcd1234", "passw0rd", "p@ssw0rd",
        "iloveyou1", "monkey123", "dragon123", "football1", "sunshine1",
        "tequiero", "hola1234", "bogota123", "medellin123", "zxcvbnm",
        "1q2w3e4r", "123qweasd", "aaaaaaaa", "11111111", "00000000",
    }
    if normalized in common_passwords:
        return "Elige una contraseña menos frecuente."
    return None


def encrypt_secret(secret, key):
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, secret.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(ciphertext, key):
    raw = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
    return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode("utf-8")


def derive_encryption_key(secret):
    return hashlib.sha256(secret.encode("utf-8")).digest()


def accounts_encryption_key():
    configured = os.environ.get("ACCOUNTS_ENCRYPTION_KEY", "").strip()
    if not configured:
        raise RuntimeError("Define ACCOUNTS_ENCRYPTION_KEY para administrar cuentas bancarias.")
    try:
        key = base64.urlsafe_b64decode(configured + "=" * (-len(configured) % 4))
    except (ValueError, TypeError) as error:
        raise RuntimeError("ACCOUNTS_ENCRYPTION_KEY debe ser una clave Base64 URL-safe de 32 bytes.") from error
    if len(key) != 32:
        raise RuntimeError("ACCOUNTS_ENCRYPTION_KEY debe codificar exactamente 32 bytes.")
    return key
