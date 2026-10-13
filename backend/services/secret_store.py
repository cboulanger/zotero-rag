"""Central gateway for secrets stored encrypted under ``<data_path>/system``.

Every process that reads or writes sensitive data (the FastAPI backend, the
indexer, CLI scripts) goes through this module instead of building Fernet
objects or resolving ``DATA_PATH`` itself. Both the encryption key
(``AUTOINDEX_SECRET``) and the data directory (``DATA_PATH``) come from the
process settings, i.e. the environment / ``.env``, so call sites pass neither.

Two kinds of secrets are managed here:

- **Zotero / embedding keys** of users, kept in the Fernet-encrypted
  auto-index key store (``autoindex_keys.json``); obtain it with
  :func:`get_key_store`.
- **Shared remote-config API keys** (``RUNPOD_API_KEY``, ``MPCDF_*_API_KEY``,
  ...) kept in ``admin_settings.json``. Values whose name satisfies
  :func:`is_secret_name` are stored as ``{"enc": "<fernet token>"}``
  (:func:`seal`) and decrypted transparently on read (:func:`unseal`); plain
  strings are legacy values that are still read and get encrypted on their
  next write. Base URLs stay plaintext.

There is no plaintext fallback: without ``AUTOINDEX_SECRET`` a secret cannot
be saved (:class:`SecretsUnavailableError`).
"""

import logging
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

ENVELOPE_KEY = "enc"
SECRET_NAME_SUFFIX = "_API_KEY"
MISSING_SECRET_MESSAGE = (
    "AUTOINDEX_SECRET is not configured; secrets cannot be stored. "
    "Generate one with: python -c \"from cryptography.fernet import Fernet; "
    "print(Fernet.generate_key().decode())\" and set AUTOINDEX_SECRET in .env "
    "or the deploy env file."
)


class SecretsUnavailableError(RuntimeError):
    """Raised when a secret must be encrypted/decrypted but ``AUTOINDEX_SECRET`` is unset."""


def _configured_secret() -> Optional[str]:
    from backend.config.settings import get_settings
    return get_settings().autoindex_secret


def get_fernet(secret: Optional[str] = None) -> Optional[Fernet]:
    """The Fernet for ``secret`` (default: the process's ``AUTOINDEX_SECRET``), or None if unset."""
    secret = secret or _configured_secret()
    return Fernet(secret.encode()) if secret else None


def secrets_enabled() -> bool:
    """True if ``AUTOINDEX_SECRET`` is configured, i.e. secrets can be stored."""
    return bool(_configured_secret())


def require_secrets_enabled() -> None:
    """Raise :class:`SecretsUnavailableError` unless secrets can be stored."""
    if not secrets_enabled():
        raise SecretsUnavailableError(MISSING_SECRET_MESSAGE)


def encrypt(plaintext: str, secret: Optional[str] = None) -> str:
    """Fernet-encrypt ``plaintext``; raises :class:`SecretsUnavailableError` without a secret."""
    fernet = get_fernet(secret)
    if fernet is None:
        raise SecretsUnavailableError(MISSING_SECRET_MESSAGE)
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt(token: str, secret: Optional[str] = None) -> Optional[str]:
    """Decrypt a Fernet token; None if no secret is configured or it doesn't match."""
    fernet = get_fernet(secret)
    if fernet is None:
        return None
    try:
        return fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        logger.error("Could not decrypt a stored secret (wrong AUTOINDEX_SECRET?)")
        return None


def is_secret_name(name: str) -> bool:
    """Whether a remote-config entry called ``name`` holds a secret (an ``*_API_KEY``)."""
    return name.upper().endswith(SECRET_NAME_SUFFIX)


def is_sealed(value: Any) -> bool:
    """Whether a stored value is an encrypted envelope."""
    return isinstance(value, dict) and isinstance(value.get(ENVELOPE_KEY), str)


def seal(value: str, secret: Optional[str] = None) -> dict:
    """Encrypt ``value`` into the on-disk envelope ``{"enc": "<token>"}``."""
    return {ENVELOPE_KEY: encrypt(value, secret)}


def unseal(stored: Any, secret: Optional[str] = None) -> Optional[str]:
    """Plaintext of a stored value: decrypts an envelope, passes a legacy
    plaintext string through, and returns None for anything undecryptable."""
    if is_sealed(stored):
        return decrypt(stored[ENVELOPE_KEY], secret)
    return stored if isinstance(stored, str) else None


def get_key_store(settings=None):
    """The auto-index key store at the configured location, keyed by the configured secret.

    ``settings`` overrides the process settings (for callers that were handed
    an explicit ``Settings`` object).
    """
    from backend.config.settings import get_settings
    from backend.services.autoindex_key_store import AutoIndexKeyStore

    settings = settings or get_settings()
    return AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
