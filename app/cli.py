"""One-off management commands run by an operator, not over HTTP.

Currently just the RBAC bootstrap: appointing the first administrator, which
cannot go through the API because every RBAC endpoint already requires
`rbac.manage`. Running it as a Python command rather than raw SQL is what
lets the appointment be written to audit_logs like any other RBAC change.

    python -m app.cli assign-admin <user_id>
"""

import argparse
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.db.session import SessionLocal
from app.modules.audit import service as audit
from app.modules.auth import repository as auth_repository
from app.modules.users import repository as rbac_repository

ADMIN_ROLE_NAME = "admin"


def assign_admin(db: Session, user_id: uuid.UUID) -> None:
    """Give an existing user the seeded admin role. Safe to run twice."""
    user = auth_repository.get_user_by_id(db, user_id)
    if user is None:
        raise NotFoundError(f"No user with id {user_id}.")

    role = rbac_repository.get_role_by_name(db, ADMIN_ROLE_NAME)
    if role is None:
        raise NotFoundError(
            "The 'admin' role does not exist. Run 'alembic upgrade head' first.",
            code="ROLE_NOT_FOUND",
        )

    if rbac_repository.get_user_role(db, user.id, role.id) is not None:
        return

    rbac_repository.add_user_role(db, user_id=user.id, role_id=role.id)
    user.authorization_version += 1
    audit.record_event(
        db,
        event_type=audit.ADMIN_BOOTSTRAPPED,
        target_user_id=user.id,
        details={"role": ADMIN_ROLE_NAME},
    )
    db.commit()


def set_account_active(db: Session, user_id: uuid.UUID, *, is_active: bool) -> None:
    """Suspend or restore an account. Disabling also ends every open session."""
    user = auth_repository.get_user_by_id(db, user_id)
    if user is None:
        raise NotFoundError(f"No user with id {user_id}.")

    if user.is_active == is_active:
        return

    user.is_active = is_active
    if not is_active:
        auth_repository.revoke_all_for_user(db, user.id, datetime.now(timezone.utc))

    audit.record_event(
        db,
        event_type=audit.ACCOUNT_ENABLED if is_active else audit.ACCOUNT_DISABLED,
        target_user_id=user.id,
    )
    db.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subcommands = parser.add_subparsers(dest="command", required=True)

    assign = subcommands.add_parser(
        "assign-admin", help="Grant the seeded admin role to an existing user."
    )
    assign.add_argument("user_id", type=uuid.UUID, help="UUID of the user to appoint.")

    disable = subcommands.add_parser("disable-account", help="Suspend an account.")
    disable.add_argument("user_id", type=uuid.UUID)

    enable = subcommands.add_parser("enable-account", help="Restore a suspended account.")
    enable.add_argument("user_id", type=uuid.UUID)

    arguments = parser.parse_args(argv)

    with SessionLocal() as db:
        if arguments.command == "assign-admin":
            assign_admin(db, arguments.user_id)
            print(f"Assigned the '{ADMIN_ROLE_NAME}' role to {arguments.user_id}.")
        else:
            is_active = arguments.command == "enable-account"
            set_account_active(db, arguments.user_id, is_active=is_active)
            print(f"Account {arguments.user_id} is now {'enabled' if is_active else 'disabled'}.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
