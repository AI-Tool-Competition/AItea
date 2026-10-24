"""Password hashing adapter; application owns account/session policy."""

import secrets

from argon2 import PasswordHasher

hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))
