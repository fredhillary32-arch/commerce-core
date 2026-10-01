"""The seed migration and the one-off script that appoints the first admin."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.exceptions import NotFoundError
from app.db.models.user import User

SEED_MIGRATION = (
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0003_seed_rbac_bootstrap.py"
)


def load_seed_migration():
    spec = importlib.util.spec_from_file_location("seed_rbac_bootstrap", SEED_MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bootstrap_counts(connection) -> tuple[int, int, int]:
    roles = connection.execute(
        text("SELECT count(*) FROM roles WHERE name = 'admin'")
    ).scalar()
    permissions = connection.execute(
        text("SELECT count(*) FROM permissions WHERE code = 'rbac.manage'")
    ).scalar()
    grants = connection.execute(
        text(
            "SELECT count(*) FROM role_permissions rp "
            "JOIN roles r ON r.id = rp.role_id "
            "JOIN permissions p ON p.id = rp.permission_id "
            "WHERE r.name = 'admin' AND p.code = 'rbac.manage'"
        )
    ).scalar()
    return roles, permissions, grants


class TestSeedMigration:
    def test_it_creates_the_admin_role_permission_and_grant(self, test_engine):
        with test_engine.connect() as connection:
            assert bootstrap_counts(connection) == (1, 1, 1)

    def test_it_creates_no_user_and_appoints_nobody(self, test_engine):
        with test_engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM users")).scalar() == 0
            assert connection.execute(text("SELECT count(*) FROM user_roles")).scalar() == 0

    def test_rerunning_it_creates_no_duplicates(self, test_engine):
        seed = load_seed_migration()

        with test_engine.begin() as connection:
            seed.seed_rbac_bootstrap(connection)
            seed.seed_rbac_bootstrap(connection)

        with test_engine.connect() as connection:
            assert bootstrap_counts(connection) == (1, 1, 1)


    def test_downgrading_is_refused_once_an_admin_has_been_appointed(
        self, db_session, test_engine
    ):
        """The documented limitation: the RESTRICT foreign key is the point.

        Rolling the seed back is only meaningful in a fresh environment, and
        Alembic's transactional DDL leaves the database untouched on failure.
        """
        from app.cli import assign_admin
        from tests.conftest import run_alembic

        user = User(email="appointed@example.com", password_hash="x")
        db_session.add(user)
        db_session.commit()
        assign_admin(db_session, user.id)

        # Down to 0002, so the rollback reaches this seed: later phases have
        # added their own migrations on top of it.
        result = run_alembic("downgrade", "0002")

        assert result.returncode != 0
        assert "foreign key" in result.stderr.lower()
        with test_engine.connect() as connection:
            assert bootstrap_counts(connection) == (1, 1, 1)
            # One transaction covers the whole downgrade, so the failure
            # leaves the database exactly where it started. Which revision
            # that is comes from the chain rather than being spelled out,
            # so a later phase adding a migration does not fail this test.
            from tests.test_alembic import script_directory

            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
                == script_directory().get_current_head()
            )


class TestAssignAdminScript:
    def test_it_grants_the_admin_role_and_records_an_audit_entry(self, db_session):
        from app.cli import assign_admin

        user = User(email="first-admin@example.com", password_hash="x")
        db_session.add(user)
        db_session.commit()

        assign_admin(db_session, user.id)

        assignment = db_session.execute(
            text(
                "SELECT r.name FROM user_roles ur JOIN roles r ON r.id = ur.role_id "
                "WHERE ur.user_id = :u"
            ),
            {"u": user.id},
        ).scalars().all()
        assert assignment == ["admin"]

        audit_rows = db_session.execute(
            text(
                "SELECT event_type, target_user_id FROM audit_logs "
                "WHERE event_type = 'rbac.bootstrap.admin_assigned'"
            )
        ).all()
        assert len(audit_rows) == 1
        assert audit_rows[0].target_user_id == user.id

    def test_it_bumps_the_authorization_version(self, db_session):
        from app.cli import assign_admin

        user = User(email="first-admin@example.com", password_hash="x")
        db_session.add(user)
        db_session.commit()

        assign_admin(db_session, user.id)

        db_session.refresh(user)
        assert user.authorization_version == 2

    def test_running_it_twice_changes_nothing_further(self, db_session):
        from app.cli import assign_admin

        user = User(email="first-admin@example.com", password_hash="x")
        db_session.add(user)
        db_session.commit()

        assign_admin(db_session, user.id)
        assign_admin(db_session, user.id)

        db_session.refresh(user)
        assert user.authorization_version == 2
        assert (
            db_session.execute(text("SELECT count(*) FROM user_roles")).scalar() == 1
        )

    def test_it_refuses_an_unknown_user(self, db_session):
        from app.cli import assign_admin

        with pytest.raises(NotFoundError):
            assign_admin(db_session, uuid.uuid4())
