"""argon2id password hashing.

Not passlib: no release since 2020-10-08 and known to break against modern bcrypt.
argon2-cffi is one dependency and three functions.

Every function here is CPU-bound for ~50-100 ms. Callers on the request path MUST run them
through `asyncio.to_thread` — this process is simultaneously streaming other users'
60-second classifications, and a synchronous hash in the event loop stalls all of them.
"""

from __future__ import annotations

import secrets
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# Library defaults (argon2id, t=3, m=64 MiB, p=4). Appropriate for ~22 interactive users;
# there is no tuning story here worth the configuration surface.
_ph = PasswordHasher()


def hash_password(raw: str) -> str:
    """Return an argon2id PHC string. There is no length cap to enforce: unlike bcrypt,
    argon2 hashes the whole input rather than silently truncating at 72 bytes."""
    return _ph.hash(raw)


def verify_password(stored_hash: str, raw: str) -> bool:
    try:
        return _ph.verify(stored_hash, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the stored hash was made with weaker parameters than the current default."""
    try:
        return _ph.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """A real hash of a random secret, verified against when the username does not exist.

    Without it, an unknown username returns in ~0 ms and a known one in ~80 ms, which turns
    the login form into a username oracle. Computed lazily so importing this module (the
    CLI does) does not cost a hash.
    """
    return hash_password(secrets.token_urlsafe(32))
