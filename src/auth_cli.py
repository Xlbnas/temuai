"""Password hashing CLI utility."""
from __future__ import annotations

import getpass

import argon2
import click


def hash_password_interactive() -> str:
    password = getpass.getpass("Enter password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        raise click.ClickException("Passwords do not match.")
    hasher = argon2.PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)
    return hasher.hash(password)


def verify_password(password: str, hash_value: str) -> bool:
    hasher = argon2.PasswordHasher()
    try:
        hasher.verify(hash_value, password)
        return True
    except argon2.exceptions.VerifyMismatchError:
        return False
