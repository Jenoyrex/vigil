"""Guards apps/api/.env.example against silently drifting from
app.config.Settings -- Phase 4A. Mechanical and narrow by design: this only
asserts every current Settings field has a corresponding `KEY=` line in the
example file, so adding a new setting without updating the template fails
this test instead of being discovered by a confused new contributor.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings

_ENV_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / ".env.example"


def _keys_in_env_example() -> set[str]:
    keys = set()
    for line in _ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0])
    return keys


def _expected_keys() -> set[str]:
    prefix = Settings.model_config["env_prefix"]
    return {f"{prefix}{field_name.upper()}" for field_name in Settings.model_fields}


def test_env_example_exists() -> None:
    assert _ENV_EXAMPLE_PATH.is_file()


def test_every_settings_field_has_an_env_example_entry() -> None:
    missing = _expected_keys() - _keys_in_env_example()
    assert not missing, f"apps/api/.env.example is missing: {sorted(missing)}"
