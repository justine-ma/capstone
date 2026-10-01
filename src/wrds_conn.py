"""WRDS connection handling, schema verification and parquet caching.

Credentials never live in tracked files. The username comes from WRDS_USERNAME
(set in the git-ignored .env); libpq reads the password from ~/.pgpass, or from
PGPASSWORD if that is set. Check the setup, and save the password on first use:

    python -m src.wrds_conn
"""

from __future__ import annotations

import difflib
import functools
import logging
import os
from collections.abc import Callable, Iterable
from pathlib import Path

import pandas as pd
import sqlalchemy as sa
import wrds
from dotenv import load_dotenv

from src.config import REPO_ROOT

log = logging.getLogger(__name__)

WRDS_HOST = "wrds-pgdata.wharton.upenn.edu"
PGPASS_PATH = Path.home() / ".pgpass"


class SchemaError(RuntimeError):
    """An expected WRDS library, table or column is not available to this account."""


def wrds_username() -> str:
    load_dotenv(REPO_ROOT / ".env")
    username = os.environ.get("WRDS_USERNAME", "").strip()
    if not username:
        raise RuntimeError("WRDS_USERNAME is not set. Copy .env.example to .env and fill it in.")
    return username


def has_saved_password(username: str) -> bool:
    """True if ~/.pgpass has an entry for this user on the WRDS host. Never reads out the password."""
    if not PGPASS_PATH.is_file():
        return False
    for line in PGPASS_PATH.read_text().splitlines():
        fields = line.split(":")
        if len(fields) >= 5 and fields[0] in (WRDS_HOST, "*") and fields[3] in (username, "*"):
            return True
    return False


def connect() -> wrds.Connection:
    """Open a WRDS connection. wrds prompts for a password only if none is saved."""
    username = wrds_username()
    if not (has_saved_password(username) or os.environ.get("PGPASSWORD")):
        log.warning("No saved WRDS password for %s; wrds will prompt for one. "
                    "Run `python -m src.wrds_conn` once to save it to ~/.pgpass.", username)
    return wrds.Connection(wrds_username=username)


class WrdsSession:
    """Connects on first use, so a run served entirely from cache never touches WRDS."""

    def __init__(self) -> None:
        self._db: wrds.Connection | None = None

    @property
    def db(self) -> wrds.Connection:
        if self._db is None:
            self._db = connect()
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self) -> WrdsSession:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def query(db: wrds.Connection, sql: str, params: dict | None = None,
          date_cols: list[str] | None = None) -> pd.DataFrame:
    """Run SQL with psycopg2-style %(name)s parameters (tuples expand for IN).

    wrds concatenates 500k-row chunks without resetting the index, so reset it here.
    """
    return db.raw_sql(sql, params=params, date_cols=date_cols).reset_index(drop=True)


def split_ref(ref: str) -> tuple[str, str]:
    library, _, table = ref.partition(".")
    if not library or not table:
        raise ValueError(f"expected 'library.table', got {ref!r}")
    return library, table


@functools.cache
def _tables(db: wrds.Connection, library: str) -> frozenset[str]:
    return frozenset(db.list_tables(library))


def verify_table(db: wrds.Connection, ref: str, columns: Iterable[str]) -> pd.DataFrame:
    """Raise SchemaError unless `ref` ("library.table") exists and has every expected column.

    Returns the describe_table output (name, nullable, type, comment).
    """
    columns = list(columns)
    library, table = split_ref(ref)
    if library not in db.list_libraries():
        raise SchemaError(f"WRDS library '{library}' (needed for {ref}) is not available to this "
                          "account: not subscribed, or renamed.")
    tables = _tables(db, library)
    if table not in tables:
        similar = difflib.get_close_matches(table, sorted(tables), n=8, cutoff=0.5)
        raise SchemaError(f"Table {ref} not found. Similar tables in '{library}': {similar or 'none'}")
    desc = db.describe_table(library, table)
    present = set(desc["name"])
    missing = [c for c in columns if c not in present]
    if missing:
        raise SchemaError(f"{ref} is missing expected columns {missing}. Columns present: {sorted(present)}")
    # Many WRDS tables are views: listed and describable, yet unreadable without the
    # underlying product's subscription. Read one row to find out now, not mid-pull.
    try:
        db.connection.exec_driver_sql(f"select 1 from {ref} limit 1").fetchall()
    except sa.exc.ProgrammingError as e:
        reason = str(e.orig).strip().splitlines()[0]
        raise SchemaError(f"{ref} exists but this account cannot read it ({reason}); "
                          "the account likely lacks the underlying subscription.") from e
    log.info("verified %s: readable, expected columns present %s", ref, columns)
    return desc


def cached(path: Path, pull: Callable[[], pd.DataFrame], refresh: bool = False) -> pd.DataFrame:
    """Read `path` if it exists; otherwise run `pull`, write the result to `path`, and return it."""
    if path.exists() and not refresh:
        log.info("cache hit: %s", path.relative_to(REPO_ROOT))
        return pd.read_parquet(path)
    df = pull()
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    log.info("cached %d rows -> %s", len(df), path.relative_to(REPO_ROOT))
    return df


if __name__ == "__main__":
    from src.config import load_config

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    cfg = load_config()
    # On first use wrds prompts for the password, then offers to create ~/.pgpass: answer y.
    db = connect()
    print("~/.pgpass entry:", "yes" if has_saved_password(wrds_username()) else "no")
    for ref in ("comp.adsprate", "comp.company", cfg.linking.ccm_table, "crsp.stocknames",
                cfg.linking.optionm_link_table, f"optionm.vsurfd{cfg.sample.options_start.year}"):
        try:
            verify_table(db, ref, [])
            print(f"  {ref:<28} readable")
        except SchemaError as e:
            print(f"  {ref:<28} NOT READABLE: {e}")
    db.close()
