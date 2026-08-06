"""Token Encryption — Fernet-based encryption for tokens.json at rest.

Prevents token theft if the file is accessed outside the application.
Derives encryption key from AUTOCLAW_TOKEN_KEY environment variable.

Usage:
  export AUTOCLAW_TOKEN_KEY=<base64-url-safe-32-byte-key>
  
  # Or generate a new key:
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

If AUTOCLAW_TOKEN_KEY is not set, tokens are stored in plaintext (backwards compatible).
"""

import os
import json
import base64
import logging

logger = logging.getLogger("autoclaw.token_encryption")

# ─── Key Management ─────────────────────────────────────────────────
_TOKEN_KEY_ENV = "AUTOCLAW_TOKEN_KEY"
_fernet = None

def _get_fernet():
    """Lazy-initialize Fernet cipher from env var."""
    global _fernet
    if _fernet is not None:
        return _fernet
    
    key_str = os.environ.get(_TOKEN_KEY_ENV)
    if not key_str:
        return None  # Encryption disabled — plaintext mode
    
    try:
        from cryptography.fernet import Fernet
        # Validate key format (must be 32 url-safe base64-encoded bytes)
        _fernet = Fernet(key_str.encode() if isinstance(key_str, str) else key_str)
        logger.info("Token encryption enabled (Fernet AES-128-CBC)")
        return _fernet
    except Exception as e:
        logger.error(f"Invalid AUTOCLAW_TOKEN_KEY: {e} — falling back to plaintext")
        return None


def encrypt_tokens(data: dict) -> bytes:
    """Encrypt token data dict to bytes.
    
    Returns JSON-then-Fernet encrypted bytes, or plaintext JSON bytes
    if encryption is not configured.
    """
    f = _get_fernet()
    plaintext = json.dumps(data, indent=2).encode('utf-8')
    if f is None:
        return plaintext  # Plaintext mode
    return f.encrypt(plaintext)


def decrypt_tokens(raw: bytes) -> dict:
    """Decrypt token data from bytes to dict.
    
    Handles both encrypted (Fernet token) and plaintext (JSON) formats
    for backwards compatibility. Returns empty dict on failure.
    """
    # Try plaintext JSON first (backwards compatible)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass  # Not plaintext — try decryption
    
    f = _get_fernet()
    if f is None:
        logger.error("Data appears encrypted but AUTOCLAW_TOKEN_KEY not set")
        return {"accounts": []}
    
    try:
        plaintext = f.decrypt(raw)
        return json.loads(plaintext.decode('utf-8'))
    except Exception as e:
        logger.error(f"Token decryption failed: {e}")
        return {"accounts": []}


def is_encryption_enabled() -> bool:
    """Check if token encryption is active."""
    return _get_fernet() is not None


def generate_key() -> str:
    """Generate a new Fernet key for AUTOCLAW_TOKEN_KEY."""
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "generate-key":
        print(f"Generated key: {generate_key()}")
        print(f"Set this as: export {_TOKEN_KEY_ENV}=<key above>")
    else:
        print(f"Usage: python token_encryption.py generate-key")
