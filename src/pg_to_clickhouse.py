"""Mirror every Postgres `cms_hospitals` table into ClickHouse for hosting.

Strategy:
  1. Introspect each PG table's columns + types via information_schema.
  2. Generate a matching ClickHouse `CREATE TABLE ... ENGINE = ReplacingMergeTree`
     with sensible primary keys (first id-ish column).
  3. Stream rows in batches via psycopg2 server-side cursor → clickhouse_connect
     `insert(...)` (which talks the native protocol over HTTPS).
  4. Type-map: jsonb → String, numeric → Float64, timestamps → DateTime64.

Usage:
    python -m src.pg_to_clickhouse                # all tables
    python -m src.pg_to_clickhouse --tables a,b   # subset
    python -m src.pg_to_clickhouse --dry-run      # print DDL only
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Iterable

import clickhouse_connect
import psycopg2.extras

from . import config
from .risk_db import connect as pg_connect

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Type mapping  (PG -> ClickHouse)
# ----------------------------------------------------------------------

PG_TO_CH = {
    "text":                       "String",
    "character varying":          "String",
    "character":                  "String",
    "bytea":                      "String",
    "uuid":                       "String",
    "boolean":                    "UInt8",
    "smallint":                   "Int16",
    "integer":                    "Int32",
    "bigint":                     "Int64",
    "real":                       "Float32",
    "double precision":           "Float64",
    "numeric":                    "Float64",
    "date":                       "Nullable(Date32)",
    "timestamp without time zone":"Nullable(DateTime64(3))",
    "timestamp with time zone":   "Nullable(DateTime64(3))",
    "json":                       "String",
    "jsonb":                      "String",
    "ARRAY":                      "Array(String)",
}


def ch_type_for(pg_type: str, udt: str) -> str:
    """Map a Postgres column type to a ClickHouse type.

    Arrays in PG come through information_schema as `ARRAY` with the element
    type in udt_name (e.g. _text). We collapse all arrays to Array(String)
    since none of our exported tables use anything more exotic."""
    if pg_type == "ARRAY":
        return "Array(String)"
    base = PG_TO_CH.get(pg_type, "String")
    # Wrap in Nullable when the PG type isn't already nullable-only and the
    # column is nullable; we use the table's `is_nullable` outside this fn.
    return base


def make_nullable(ct: str, nullable: bool) -> str:
    if not nullable or ct.startswith("Nullable("):
        return ct
    if ct.startswith("Array(") or ct == "UInt8":
        return ct  # arrays + bools shouldn't be wrapped
    return f"Nullable({ct})"


# ----------------------------------------------------------------------
# Introspection
# ----------------------------------------------------------------------

def list_pg_tables(pg) -> list[str]:
    with pg.cursor() as cur:
        cur.execute("""SELECT tablename FROM pg_tables
                       WHERE schemaname='public' ORDER BY tablename""")
        return [r[0] for r in cur.fetchall()]


def pg_columns(pg, table: str) -> list[tuple[str, str, str, bool]]:
    """Return [(name, data_type, udt_name, is_nullable)]."""
    with pg.cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type, udt_name, (is_nullable='YES')
              FROM information_schema.columns
             WHERE table_schema='public' AND table_name=%s
             ORDER BY ordinal_position""", (table,))
        return cur.fetchall()


def pg_count(pg, table: str) -> int:
    with pg.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{table}"')
        return cur.fetchone()[0]


# ----------------------------------------------------------------------
# DDL + insert
# ----------------------------------------------------------------------

def make_ch_ddl(table: str, cols: list[tuple]) -> tuple[str, list[str]]:
    """Build CREATE TABLE for ClickHouse. Returns (ddl, ordered_col_names)."""
    parts = []
    names = []
    for name, ptype, udt, nullable in cols:
        ct = ch_type_for(ptype, udt)
        ct = make_nullable(ct, nullable)
        parts.append(f'  `{name}` {ct}')
        names.append(name)
    pk = names[0]
    ddl = (
        f"CREATE TABLE IF NOT EXISTS `{table}` (\n" +
        ",\n".join(parts) +
        f"\n) ENGINE = ReplacingMergeTree() ORDER BY ({pk})"
    )
    return ddl, names


def coerce_value(v, ptype: str):
    """Make a PG value safe to send to ClickHouse via clickhouse_connect."""
    if v is None:
        return None
    if ptype in ("json", "jsonb"):
        # Already a dict/list from psycopg2 jsonb adapter — re-stringify
        if isinstance(v, (dict, list)):
            return json.dumps(v, default=str)
        return str(v)
    if ptype == "ARRAY":
        # psycopg2 returns native Python list
        return [str(x) if x is not None else "" for x in (v or [])]
    return v


def stream_table(pg, table: str, cols: list[tuple],
                 batch_size: int = 500) -> Iterable[list[list]]:
    """Stream rows from a PG table in batches, value-coerced for CH."""
    col_names = ", ".join(f'"{c[0]}"' for c in cols)
    types     = [c[1] for c in cols]
    with pg.cursor(name=f"export_{table}") as cur:  # server-side cursor
        cur.itersize = batch_size
        cur.execute(f'SELECT {col_names} FROM "{table}"')
        batch = []
        for row in cur:
            batch.append([coerce_value(v, ptypes) for v, ptypes in zip(row, types)])
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------

def ch_client():
    return clickhouse_connect.get_client(
        host=config.CLICKHOUSE_HOST,
        port=config.CLICKHOUSE_PORT,
        username=config.CLICKHOUSE_USER,
        password=config.CLICKHOUSE_PASSWORD,
        database=config.CLICKHOUSE_DATABASE,
        secure=config.CLICKHOUSE_SECURE,
    )


def ch_count(ch, table: str) -> int:
    try:
        r = ch.command(f"SELECT count() FROM `{table}`")
        return int(r) if r is not None else 0
    except Exception:
        return -1


def migrate(tables: list[str] | None = None,
            skip: set[str] | None = None,
            dry_run: bool = False,
            recreate: bool = False,
            resume: bool = False) -> dict:
    skip = skip or set()
    summary: dict = {}
    with pg_connect() as pg:
        all_tables = list_pg_tables(pg)
        if tables:
            all_tables = [t for t in all_tables if t in set(tables)]
        all_tables = [t for t in all_tables if t not in skip]

        ch = None if dry_run else ch_client()
        if ch is not None:
            ch.command(f"CREATE DATABASE IF NOT EXISTS `{config.CLICKHOUSE_DATABASE}`")

        for table in all_tables:
            cols = pg_columns(pg, table)
            n = pg_count(pg, table)
            ddl, col_names = make_ch_ddl(table, cols)
            log.info("=== %s (%d rows) ===", table, n)
            if dry_run:
                print(ddl, ";\n", sep="")
                summary[table] = {"rows_pg": n, "status": "dry_run"}
                continue
            if resume:
                already = ch_count(ch, table)
                if already == n and n > 0:
                    log.info("  resume: skipping (CH already has %d rows)", already)
                    summary[table] = {"rows_pg": n, "rows_inserted": already, "status": "skip-resumed"}
                    continue
            if recreate:
                ch.command(f"DROP TABLE IF EXISTS `{table}`")
            ch.command(ddl)
            if n == 0:
                summary[table] = {"rows_pg": 0, "rows_inserted": 0, "status": "ok"}
                continue
            # Auto-shrink batch size for wide tables (likely jsonb-heavy)
            wide = any(c[1] in ("jsonb", "json") for c in cols)
            bs = 200 if wide else 500
            inserted = 0
            attempts_left = 3
            for batch in stream_table(pg, table, cols, batch_size=bs):
                while True:
                    try:
                        ch.insert(table, batch, column_names=col_names)
                        inserted += len(batch)
                        break
                    except Exception as e:
                        attempts_left -= 1
                        log.warning("  insert error (attempts left=%d): %s", attempts_left, str(e)[:200])
                        if attempts_left <= 0:
                            raise
                        # smaller next time
                        bs = max(50, bs // 2)
                        log.warning("  retrying with batch=%d", bs)
            summary[table] = {"rows_pg": n, "rows_inserted": inserted, "status": "ok"}
            log.info("  inserted %d / %d", inserted, n)
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tables", help="Comma-separated subset; default: all public tables")
    p.add_argument("--skip", help="Comma-separated table names to skip", default="")
    p.add_argument("--dry-run", action="store_true", help="Print DDL only")
    p.add_argument("--recreate", action="store_true", help="DROP + CREATE in CH")
    p.add_argument("--resume", action="store_true",
                   help="Skip tables where CH count already matches PG count")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    tables = [t.strip() for t in args.tables.split(",")] if args.tables else None
    skip = {t.strip() for t in args.skip.split(",") if t.strip()}
    out = migrate(tables=tables, skip=skip, dry_run=args.dry_run,
                  recreate=args.recreate, resume=args.resume)
    print("\n=== migration summary ===")
    for t, info in out.items():
        print(f"  {t:45s} {info}")


if __name__ == "__main__":
    main()
