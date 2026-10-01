"""Read-only schema fingerprint of the legacy ``ecommerce`` database.

The legacy database must never be migrated, altered, dropped, truncated,
seeded or written to. This script is the **only** sanctioned contact with
it: it takes a digest of its schema before and after each phase, so that
"we did not touch it" is evidence rather than an assurance.

--------------------------------------------------------------------------
Why this file exists at all
--------------------------------------------------------------------------

Phases 2, 3 and 4 each took a fingerprint from a throwaway script that was
not kept. By Phase 5 the method had been lost, so Phase 4's recorded digest
``125504e1…`` could not be reproduced and the two phases' digests were not
comparable - only the counts were. A digest whose method is not written
down is not a chain of custody. The serialisation below is therefore part
of the contract, and changing it breaks comparability with every digest
taken after this file was written. If it ever must change, record the
change and re-baseline explicitly, as ``legacy-db-fingerprint.md`` does for
the Phase 4/5 break.

--------------------------------------------------------------------------
Read-only by construction, not by intention
--------------------------------------------------------------------------

Three independent guards, so that a mistake in any one of them is not
enough to write to the legacy database:

1. The database name in the resolved URL must be exactly ``ecommerce``.
   ``commerce_platform`` and ``commerce_platform_test`` are refused by
   name, and so is anything else - this script fingerprints one database.
2. The connection is opened with ``default_transaction_read_only = on``, so
   PostgreSQL itself rejects any write on it. The script asserts that
   ``transaction_read_only`` really is ``on`` before it reads anything; if
   the option were dropped, it stops rather than continuing unprotected.
3. Every query reads ``information_schema`` or ``pg_catalog`` only. No
   application table is named anywhere in this file, so no customer data is
   read, logged or hashed.

--------------------------------------------------------------------------
The serialisation, stated so it can never be lost again
--------------------------------------------------------------------------

The digest is **SHA-256** over the UTF-8 encoding of the following text,
built from three sections emitted in this fixed order: ``tables``,
``columns``, ``constraints``.

Each section contributes::

    "## " + <section name> + "\\n"

followed by one line per row, in the order the section's query returns them
(every query carries an explicit ``ORDER BY``, so the order does not depend
on the planner). Each row line is::

    <field> + "\\x1f" + <field> + ... + "\\n"

with every field rendered by ``str()``. ``\\x1f`` is the ASCII unit
separator, chosen because it cannot occur in a PostgreSQL identifier or
type name, so no field value can forge a separator.

The three sections, with their exact fields:

``tables``
    From ``information_schema.tables`` where ``table_schema = 'public'``
    and ``table_type = 'BASE TABLE'``, ordered by ``table_name``.
    Fields: ``table_name``.

``columns``
    From ``information_schema.columns`` where ``table_schema = 'public'``,
    ordered by ``table_name, column_name``. Fields: ``table_name``,
    ``column_name``, ``data_type``, ``is_nullable``,
    ``coalesce(column_default, '')``.

``constraints``
    From ``pg_constraint`` joined to ``pg_class`` and ``pg_namespace``
    where ``nspname = 'public'``, ordered by ``relname, conname``.
    Fields: ``relname``, ``conname``, ``contype``.

Ordering is by name rather than by ``oid`` deliberately: an oid changes if
an object is recreated identically, which would report a difference that is
not one.

--------------------------------------------------------------------------
Usage
--------------------------------------------------------------------------

::

    uv run python scripts/fingerprint_legacy_db.py

The connection URL is derived from ``DATABASE_URL`` - taken from the
environment if set, otherwise read from ``.env`` - with its database name
replaced by ``ecommerce``, so no credential is ever typed on a command line
or committed. Pass ``--url`` to override explicitly; it is subject to the
same ``ecommerce``-only check.

Exit status is 0 when the fingerprint was taken, 1 when it was refused.
"""

import argparse
import hashlib
import pathlib
import re
import sys

from sqlalchemy import create_engine, text

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

# The one database this script may open. Anything else is refused, which
# covers the two application databases without having to enumerate them.
LEGACY_DATABASE_NAME = "ecommerce"

# ASCII unit separator. Cannot appear in an identifier or a type name, so a
# field value can never forge a field boundary.
FIELD_SEPARATOR = "\x1f"

HASH_ALGORITHM = "sha256"

SECTIONS: tuple[tuple[str, str], ...] = (
    (
        "tables",
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
        "ORDER BY table_name",
    ),
    (
        "columns",
        "SELECT table_name, column_name, data_type, is_nullable, "
        "coalesce(column_default, '') FROM information_schema.columns "
        "WHERE table_schema = 'public' "
        "ORDER BY table_name, column_name",
    ),
    (
        "constraints",
        "SELECT rel.relname, con.conname, con.contype FROM pg_constraint con "
        "JOIN pg_class rel ON rel.oid = con.conrelid "
        "JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace "
        "WHERE nsp.nspname = 'public' "
        "ORDER BY rel.relname, con.conname",
    ),
)


class Refused(Exception):
    """The script declined to run. Never a partial read."""


def read_database_url() -> str:
    """The development URL, from the environment or ``.env``."""
    import os

    from_environment = os.environ.get("DATABASE_URL")
    if from_environment:
        return from_environment.strip()

    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        raise Refused("DATABASE_URL is not set and there is no .env to read it from.")

    match = re.search(r"^DATABASE_URL=(.+)$", env_file.read_text(), re.M)
    if match is None:
        raise Refused(".env contains no DATABASE_URL.")
    return match.group(1).strip()


def legacy_url(source_url: str) -> str:
    """Point an application URL at the legacy database, keeping credentials."""
    return re.sub(r"/[^/?]+(\?|$)", f"/{LEGACY_DATABASE_NAME}\\1", source_url, count=1)


def database_name_of(url: str) -> str:
    match = re.search(r"/([^/?]+)(\?|$)", url)
    if match is None:
        raise Refused(f"Could not read a database name from {redact(url)}.")
    return match.group(1)


def redact(url: str) -> str:
    """A URL safe to print: the password never appears in output or logs."""
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


def require_legacy_database(url: str) -> None:
    """Guard 1: this script opens exactly one database, by name."""
    name = database_name_of(url)
    if name != LEGACY_DATABASE_NAME:
        raise Refused(
            f"Refusing to run against {name!r}. This script only ever reads "
            f"the legacy {LEGACY_DATABASE_NAME!r} database; the application "
            "databases are never fingerprinted and never touched by it."
        )


def open_read_only(url: str):
    """Guard 2: a connection PostgreSQL will not accept a write on."""
    return create_engine(
        url,
        future=True,
        # Server-side, so it holds for every statement on this connection
        # regardless of what the rest of this file does.
        connect_args={"options": "-c default_transaction_read_only=on"},
    )


def fingerprint(url: str) -> tuple[dict[str, int], str]:
    """Return per-section row counts and the digest over all of them."""
    engine = open_read_only(url)
    digest = hashlib.new(HASH_ALGORITHM)
    counts: dict[str, int] = {}

    try:
        with engine.connect() as connection:
            read_only = connection.execute(text("SHOW transaction_read_only")).scalar_one()
            if read_only != "on":
                raise Refused(
                    "The connection is not read-only "
                    f"(transaction_read_only = {read_only!r}); refusing to continue."
                )

            for name, statement in SECTIONS:
                digest.update(f"## {name}\n".encode())
                rows = connection.execute(text(statement)).all()
                counts[name] = len(rows)
                for row in rows:
                    rendered = FIELD_SEPARATOR.join(str(field) for field in row)
                    digest.update((rendered + "\n").encode())
    finally:
        engine.dispose()

    return counts, digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--url",
        help=(
            "Explicit connection URL. Subject to the same checks; it must "
            f"name the {LEGACY_DATABASE_NAME!r} database."
        ),
    )
    arguments = parser.parse_args(argv)

    try:
        url = arguments.url or legacy_url(read_database_url())
        require_legacy_database(url)
        counts, digest = fingerprint(url)
    except Refused as refusal:
        print(f"REFUSED: {refusal}", file=sys.stderr)
        return 1

    print(f"database   : {redact(url)}")
    print(f"tables     : {counts['tables']}")
    print(f"columns    : {counts['columns']}")
    print(f"constraints: {counts['constraints']}")
    print(f"{HASH_ALGORITHM}     : {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
