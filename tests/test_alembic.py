import os
import subprocess
import sys
from pathlib import Path

from app.core.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_alembic(*args: str) -> subprocess.CompletedProcess:
    settings = get_settings()
    env = os.environ.copy()
    # Migrations must run against the dedicated test database, never the
    # dev DATABASE_URL and never "ecommerce".
    env["DATABASE_URL"] = settings.test_database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def test_alembic_upgrade_downgrade_upgrade_cycle():
    upgrade_result = _run_alembic("upgrade", "head")
    assert upgrade_result.returncode == 0, upgrade_result.stderr

    current_result = _run_alembic("current")
    assert current_result.returncode == 0, current_result.stderr
    assert "0001" in current_result.stdout

    downgrade_result = _run_alembic("downgrade", "base")
    assert downgrade_result.returncode == 0, downgrade_result.stderr

    reupgrade_result = _run_alembic("upgrade", "head")
    assert reupgrade_result.returncode == 0, reupgrade_result.stderr
