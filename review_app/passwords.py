"""Salted password credentials; no plaintext passwords are stored."""
import hashlib
import hmac
import secrets

INITIAL_PASSWORD = '12345678'
ITERATIONS = 600_000


def valid_password(value):
    return isinstance(value, str) and 8 <= len(value) <= 128 and not any(ord(c) < 32 for c in value)


def hash_password(password):
    if not valid_password(password):
        raise ValueError('密码须为 8–128 个字符，不含控制字符')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}'


def verify_password(password, encoded):
    if not valid_password(password):
        return False
    try:
        algorithm, iterations, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or int(iterations) != ITERATIONS or len(salt) != 32 or len(expected) != 64:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (AttributeError, ValueError, TypeError):
        return False
