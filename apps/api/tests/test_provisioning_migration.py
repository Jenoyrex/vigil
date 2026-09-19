"""Upgrade/downgrade verification for
alembic/versions/2925f09e0276_add_provisioning_bootstrap.py (Phase 4D, F3)
and alembic/versions/ad0d41d7d82c_add_dashboard_sessions.py (Phase 4D, F1)
against a real PostgreSQL database.

Invokes `alembic` as a subprocess with `VIGIL_API_DATABASE_URL` pinned to
the test database explicitly -- never the in-process `app.config.settings`
singleton (which defaults to the `vigil` development database and must
never be written to by anything in this test suite; see
test_provisioning.py's `_ConcurrentTestSessionLocal` comment for the exact
same hazard in a different test). This also matches how an operator
actually runs migrations (`uv run alembic upgrade head`), rather than
alembic's Python API, which would need its own environment-variable
sleight of hand to avoid the identical wrong-database risk.

Always restores `head` before returning, even on failure -- every other
test in this suite (`db_session`, per conftest.py) expects
`provisioning_bootstrap` to already exist.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

_API_ROOT = Path(__file__).resolve().parent.parent
_TEST_DATABASE_URL = os.environ.get(
    "VIGIL_API_TEST_DATABASE_URL",
    "postgresql+psycopg://vigil:vigil@localhost:5434/vigil_test",
)


def _run_alembic(*args: str) -> None:
    env = {
        **os.environ,
        "VIGIL_API_DATABASE_URL": _TEST_DATABASE_URL,
        "VIGIL_API_INTERNAL_SERVICE_TOKEN": os.environ.get(
            "VIGIL_API_INTERNAL_SERVICE_TOKEN", "test-internal-service-token"
        ),
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_API_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _table_exists(name: str) -> bool:
    engine = create_engine(_TEST_DATABASE_URL)
    try:
        return inspect(engine).has_table(name)
    finally:
        engine.dispose()


def test_provisioning_bootstrap_migration_upgrade_downgrade_upgrade() -> None:
    _run_alembic("upgrade", "head")
    assert _table_exists("provisioning_bootstrap")

    try:
        # Explicit target revision, not "-1": a later migration
        # (ad0d41d7d82c, dashboard_sessions) now sits on top of this one at
        # head, so "-1" from head would only revert that later migration,
        # not this one. "18f8ea1539cc" is 2925f09e0276's own down_revision
        # -- downgrading to it necessarily reverts everything after,
        # including 2925f09e0276 itself.
        _run_alembic("downgrade", "18f8ea1539cc")
        assert not _table_exists("provisioning_bootstrap")
    finally:
        _run_alembic("upgrade", "head")

    assert _table_exists("provisioning_bootstrap")


def test_dashboard_sessions_migration_upgrade_downgrade_upgrade() -> None:
    _run_alembic("upgrade", "head")
    assert _table_exists("dashboard_sessions")

    try:
        _run_alembic("downgrade", "2925f09e0276")
        assert not _table_exists("dashboard_sessions")
        # The migration below it in the chain is untouched by this
        # downgrade -- proves the downgrade() only reverted its own table.
        assert _table_exists("provisioning_bootstrap")
    finally:
        _run_alembic("upgrade", "head")

    assert _table_exists("dashboard_sessions")
