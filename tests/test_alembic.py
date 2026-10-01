"""The migration chain, asserted structurally rather than by revision id.

These tests deliberately name no revision. A test that hardcodes "the head
is 0005" fails on the day a new phase adds 0006 - not because anything is
wrong, but because the test was written about a moment rather than about a
property. What actually has to stay true is that the chain is a single
unbranched line with no gaps, that it runs forward from nothing, and that it
runs backward again.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.conftest import PROJECT_ROOT, run_alembic

VERSIONS_DIRECTORY = PROJECT_ROOT / "alembic" / "versions"


def script_directory() -> ScriptDirectory:
    """Alembic's own view of the chain, read from disk without running it."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def revision_files() -> list[Path]:
    return [path for path in VERSIONS_DIRECTORY.glob("*.py") if path.name != "__init__.py"]


def test_the_chain_has_exactly_one_head_and_one_base():
    script = script_directory()

    assert len(script.get_heads()) == 1
    assert len(script.get_bases()) == 1


def test_the_chain_is_unbranched_and_has_no_gaps():
    """Every migration file is reachable in one line from base to head."""
    script = script_directory()
    walked = list(script.walk_revisions("base", script.get_current_head()))

    # A file that is not on the walk is an orphan or sits across a gap.
    assert len(walked) == len(revision_files())

    for revision in walked:
        # A tuple down_revision is a merge point, which would make the
        # history a graph rather than a line.
        assert not isinstance(revision.down_revision, tuple), revision.revision
        assert not revision.branch_labels, revision.revision


def test_upgrading_from_nothing_reaches_the_head():
    assert run_alembic("downgrade", "base").returncode == 0

    upgrade_result = run_alembic("upgrade", "head")
    assert upgrade_result.returncode == 0, upgrade_result.stderr

    current_result = run_alembic("current")
    assert current_result.returncode == 0, current_result.stderr
    assert script_directory().get_current_head() in current_result.stdout


def test_the_whole_chain_rolls_back_and_forward_again():
    assert run_alembic("upgrade", "head").returncode == 0

    downgrade_result = run_alembic("downgrade", "base")
    assert downgrade_result.returncode == 0, downgrade_result.stderr

    reupgrade_result = run_alembic("upgrade", "head")
    assert reupgrade_result.returncode == 0, reupgrade_result.stderr


def test_each_migration_steps_back_and_forward_one_at_a_time():
    """Every individual downgrade works, not just a single jump to base.

    A phase whose downgrade only happens to succeed as part of a full
    teardown is not actually reversible, and the failure would surface for
    the first time during a real rollback.
    """
    assert run_alembic("upgrade", "head").returncode == 0

    for _ in revision_files():
        result = run_alembic("downgrade", "-1")
        assert result.returncode == 0, result.stderr

    for _ in revision_files():
        result = run_alembic("upgrade", "+1")
        assert result.returncode == 0, result.stderr

    assert script_directory().get_current_head() in run_alembic("current").stdout
