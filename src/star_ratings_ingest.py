"""Medicare Part C/D Star Ratings ingester.

Source: cms.gov/medicare/health-drug-plans/part-c-d-performance-data
Annual ZIP at /files/zip/{year}-star-ratings-data-tables.zip

We ingest the 'Summary Ratings' CSV — one row per Contract with per-program
summary stars (Part C, Part D, Overall). Wide-format measure-level stars
are skipped for now (~50 measure columns per contract).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile

import requests

from . import db

log = logging.getLogger(__name__)

DATASET = {"id": "ma_star_ratings", "name": "Medicare Part C/D Star Ratings"}
ZIP_TEMPLATE = "https://www.cms.gov/files/zip/{year}-star-ratings-data-tables.zip"

COLS = [
    "rating_year", "contract_id", "measure_id", "measure_name",
    "star_rating", "raw_score", "source_url", "raw",
]
COL_TYPES = [
    "UInt16", "String", "String", "Nullable(String)",
    "Nullable(Float32)", "Nullable(String)", "Nullable(String)", "String",
]


def _star(v):
    if v is None:
        return None
    s = str(v).strip().rstrip(" ")
    if not s or "not applicable" in s.lower() or "not enough" in s.lower():
        return None
    try:
        return float(s)
    except ValueError:
        # Sometimes encoded "5 out of 5"
        m = re.match(r"^(\d+(\.\d+)?)\s", s)
        return float(m.group(1)) if m else None


def find_summary_csv(zip_bytes: bytes) -> tuple[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for n in z.namelist():
            if "Summary Ratings" in n and n.lower().endswith(".csv"):
                return n, z.read(n)
    raise RuntimeError("No 'Summary Ratings' CSV in star-ratings ZIP")


def parse_summary(csv_bytes: bytes, year: int, source_url: str):
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    # Layout: row 0 = report title, row 1 = column names, then data.
    if len(rows) < 3:
        return
    header = [h.strip() for h in rows[1]]
    # Locate dynamic column indices
    def find_col(*needles):
        for i, h in enumerate(header):
            hl = h.lower()
            if all(n in hl for n in needles):
                return i
        return -1
    idx_contract = find_col("contract number")
    idx_part_c = find_col(f"{year}", "part c summary")
    idx_part_d = find_col(f"{year}", "part d summary")
    idx_overall = find_col(f"{year}", "overall")
    for row in rows[2:]:
        if not row or len(row) <= idx_contract:
            continue
        contract = (row[idx_contract] if idx_contract >= 0 else "").strip()
        if not contract:
            continue
        rec = dict(zip(header, row))
        # Yield up to 3 measure rows per contract for Part C / D / Overall
        for measure_id, col_idx, label in (
            ("part_c_summary", idx_part_c, "Part C Summary"),
            ("part_d_summary", idx_part_d, "Part D Summary"),
            ("overall", idx_overall, "Overall Rating"),
        ):
            if col_idx < 0 or col_idx >= len(row):
                continue
            raw_val = row[col_idx]
            yield (
                year, contract, measure_id, label,
                _star(raw_val), str(raw_val).strip(),
                source_url, json.dumps(rec, ensure_ascii=False),
            )


def ingest_year(year: int = 2026, batch_size: int = 5000) -> tuple[int, int]:
    url = ZIP_TEMPLATE.format(year=year)
    log.info("Downloading %s", url)
    r = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    log.info("Downloaded %d bytes", len(r.content))
    name, csv_bytes = find_summary_csv(r.content)
    log.info("Extracted %s (%d bytes)", name, len(csv_bytes))
    rows = list(parse_summary(csv_bytes, year, url))
    log.info("Parsed %d star-rating rows for %d", len(rows), year)

    # Dedup by (year, contract, measure_id)
    seen: dict[tuple, tuple] = {}
    for r in rows:
        seen[(r[0], r[1], r[2])] = r
    rows = list(seen.values())

    fetched = len(rows)
    upserted = 0
    status = "success"
    error: str | None = None
    with db.connect() as conn:
        handle = db.start_ingest_log(conn, DATASET)
        try:
            for i in range(0, len(rows), batch_size):
                chunk = rows[i : i + batch_size]
                conn.insert("medicare_star_ratings", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
            log.info("Star Ratings %d upserted %d rows", year, upserted)
        except Exception as e:
            status = "error"
            error = repr(e)
            raise
        finally:
            db.finish_ingest_log(conn, handle, fetched, upserted, status, error)
    return fetched, upserted


def ingest(years=(2023, 2024, 2025, 2026)) -> tuple[int, int]:
    total_f = total_u = 0
    for y in years:
        try:
            f, u = ingest_year(y)
            total_f += f
            total_u += u
        except Exception as e:
            log.warning("Star Ratings %d skipped: %s", y, e)
    return total_f, total_u


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    f, u = ingest()
    print(f"Done. fetched={f}, upserted={u}")
