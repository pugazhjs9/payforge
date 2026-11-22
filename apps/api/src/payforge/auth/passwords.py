"""Password hashing with argon2id.

argon2id is deliberately slow and memory-hungry, so a leaked database can't be
brute-forced quickly. Parameters follow OWASP's recommendation (19 MiB, 2 passes).
Hashing is CPU-bound, so it runs in a worker thread: on the event loop it would
pause every other request while it computes.
"""

import asyncio
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


def _verify(stored: str, password: str) -> bool:
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def verify_password(stored: str, password: str) -> bool:
    return await asyncio.to_thread(_verify, stored, password)


# A real hash of a random password. Login verifies against it when the email doesn't
# exist, so "unknown email" and "wrong password" take the same time.
DUMMY_HASH = _hasher.hash(secrets.token_hex(16))
