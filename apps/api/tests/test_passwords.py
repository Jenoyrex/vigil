"""Tests for app.security.passwords -- scrypt password hashing for
dashboard user authentication (Phase 4D, F1)."""

from __future__ import annotations

from app.security.passwords import DUMMY_HASH_FOR_TIMING, hash_password, verify_password


def test_hash_password_produces_a_scrypt_encoded_string() -> None:
    encoded = hash_password("correct horse battery staple")
    parts = encoded.split("$")
    assert len(parts) == 6
    scheme, n, r, p, salt_b64, hash_b64 = parts
    assert scheme == "scrypt"
    assert n.startswith("n=")
    assert r.startswith("r=")
    assert p.startswith("p=")
    assert salt_b64
    assert hash_b64


def test_hash_password_never_equals_the_plaintext() -> None:
    password = "correct horse battery staple"
    encoded = hash_password(password)
    assert encoded != password
    assert password not in encoded


def test_hash_password_uses_a_random_salt_each_call() -> None:
    """Same password, two calls -- two different encoded strings (and two
    different embedded salts), proving a fresh random salt is drawn every
    time rather than reused."""
    password = "correct horse battery staple"
    first = hash_password(password)
    second = hash_password(password)
    assert first != second

    first_salt = first.split("$")[4]
    second_salt = second.split("$")[4]
    assert first_salt != second_salt


def test_verify_password_accepts_the_correct_password() -> None:
    password = "correct horse battery staple"
    encoded = hash_password(password)
    assert verify_password(password, encoded) is True


def test_verify_password_rejects_a_wrong_password() -> None:
    encoded = hash_password("correct horse battery staple")
    assert verify_password("wrong password entirely", encoded) is False


def test_verify_password_is_case_sensitive() -> None:
    encoded = hash_password("Password123!")
    assert verify_password("password123!", encoded) is False


def test_verify_password_rejects_empty_string_against_a_real_hash() -> None:
    encoded = hash_password("correct horse battery staple")
    assert verify_password("", encoded) is False


def test_verify_password_fails_safe_on_malformed_hash_wrong_scheme() -> None:
    assert verify_password("anything", "not-scrypt$n=1$r=1$p=1$AA==$AA==") is False


def test_verify_password_fails_safe_on_malformed_hash_missing_fields() -> None:
    assert verify_password("anything", "scrypt$n=16384$r=8") is False


def test_verify_password_fails_safe_on_malformed_hash_non_numeric_params() -> None:
    assert verify_password("anything", "scrypt$n=abc$r=8$p=1$AA==$AA==") is False


def test_verify_password_fails_safe_on_malformed_hash_invalid_base64() -> None:
    assert verify_password("anything", "scrypt$n=16384$r=8$p=1$not-valid-base64!!!$AA==") is False


def test_verify_password_fails_safe_on_completely_garbage_input() -> None:
    assert verify_password("anything", "") is False
    assert verify_password("anything", "garbage") is False


def test_verify_password_fails_safe_on_out_of_range_kdf_parameters() -> None:
    # n=0 is invalid for hashlib.scrypt -- must fail closed, not raise.
    assert verify_password("anything", "scrypt$n=0$r=8$p=1$AA==$AA==") is False


def test_verify_password_honors_parameters_embedded_in_the_hash() -> None:
    """A hash produced with non-default parameters must still verify
    correctly -- app.security.passwords always re-derives using the
    parameters recorded in the presented hash, never this module's
    current constants, so a future default-parameter change never
    invalidates already-issued hashes."""
    import base64
    import hashlib
    import secrets

    password = "correct horse battery staple"
    salt = secrets.token_bytes(16)
    n, r, p = 2**10, 4, 1  # deliberately different from the module defaults
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    encoded = (
        f"scrypt$n={n}$r={r}$p={p}$"
        f"{base64.b64encode(salt).decode('ascii')}$"
        f"{base64.b64encode(derived).decode('ascii')}"
    )
    assert verify_password(password, encoded) is True
    assert verify_password("wrong", encoded) is False


def test_dummy_hash_for_timing_is_a_valid_encoded_hash_that_verifies_against_no_real_password() -> (
    None
):
    """Used to keep login timing uniform for an unknown email -- must be a
    structurally valid hash (so verify_password does real scrypt work
    against it, not an early parse-failure short-circuit), but must not
    match any real password used in this test suite."""
    parts = DUMMY_HASH_FOR_TIMING.split("$")
    assert len(parts) == 6
    assert parts[0] == "scrypt"
    assert verify_password("correct horse battery staple", DUMMY_HASH_FOR_TIMING) is False
