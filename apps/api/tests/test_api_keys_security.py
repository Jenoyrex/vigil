"""Unit tests for app/security/api_keys.py hashing/shape-check helpers."""

from __future__ import annotations

from app.security.api_keys import generate_api_key, has_expected_key_shape, hash_api_key


def test_generate_api_key_hash_matches_recomputed_hash() -> None:
    raw_key, key_prefix, key_hash = generate_api_key()

    assert raw_key.startswith(key_prefix)
    assert hash_api_key(raw_key) == key_hash


def test_generate_api_key_is_unique_per_call() -> None:
    raw_key_a, _, hash_a = generate_api_key()
    raw_key_b, _, hash_b = generate_api_key()

    assert raw_key_a != raw_key_b
    assert hash_a != hash_b


def test_has_expected_key_shape_accepts_generated_key() -> None:
    raw_key, _, _ = generate_api_key()
    assert has_expected_key_shape(raw_key) is True


def test_has_expected_key_shape_rejects_garbage() -> None:
    assert has_expected_key_shape("not-a-vigil-key") is False
    assert has_expected_key_shape("") is False
    assert has_expected_key_shape("vgl_onlyprefix") is False
    assert has_expected_key_shape("vgl_prefix.") is False
