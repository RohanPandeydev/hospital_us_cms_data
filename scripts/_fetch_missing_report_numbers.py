"""Fast: pull just REPORT_NUMBER for the 138 missing MDR_REPORT_KEYs.

Uses DuckDB column projection so each parquet is read for only 2 of its
86 columns — way faster than the original SELECT *."""

import json
import os
import sys
import time

import duckdb
from dotenv import load_dotenv

load_dotenv()

d = json.load(open("downloads/mdrfoi_diff/missing.json"))
missing_keys = d["missing_keys"]
print(f"looking up REPORT_NUMBER for {len(missing_keys)} MDR_REPORT_KEYs", flush=True)

con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs;")
con.execute(
    "SET s3_region='us-east-1';"
    f"SET s3_access_key_id='{os.getenv('AWS_ACCESS_KEY_ID')}';"
    f"SET s3_secret_access_key='{os.getenv('AWS_SECRET_ACCESS_KEY')}';"
)
bucket = os.getenv("AWS_BUCKET")

con.execute("CREATE TABLE missing_keys (k VARCHAR)")
con.executemany("INSERT INTO missing_keys VALUES (?)",
                [(k,) for k in missing_keys])

t0 = time.time()
df = con.execute(f"""
  SELECT DISTINCT p.MDR_REPORT_KEY, p.REPORT_NUMBER, p.DATE_RECEIVED,
         p.EVENT_TYPE, p.MANUFACTURER_NAME, p.MANUFACTURER_G1_NAME
  FROM read_parquet('s3://{bucket}/us-data/maude-data/mdrfoi/partition=*/*.parquet',
                    hive_partitioning=true) p
  JOIN missing_keys m ON p.MDR_REPORT_KEY = m.k
""").fetchdf()
print(f"scan done in {time.time()-t0:.1f}s, rows fetched: {len(df)}", flush=True)

out = []
for _, r in df.iterrows():
    out.append({
        "mdr_report_key": str(r["MDR_REPORT_KEY"]),
        "report_number": (str(r["REPORT_NUMBER"])
                          if r["REPORT_NUMBER"] is not None and str(r["REPORT_NUMBER"]) != "nan"
                          else None),
        "date_received": (str(r["DATE_RECEIVED"]) if r["DATE_RECEIVED"] is not None else None),
        "event_type": (str(r["EVENT_TYPE"]) if r["EVENT_TYPE"] is not None else None),
        "manufacturer": (str(r["MANUFACTURER_NAME"])
                         if r["MANUFACTURER_NAME"] not in (None, "")
                         else (str(r["MANUFACTURER_G1_NAME"])
                               if r["MANUFACTURER_G1_NAME"] is not None else None)),
    })

with open("downloads/mdrfoi_diff/missing_report_numbers.json", "w") as f:
    json.dump({
        "missing_count": len(missing_keys),
        "fetched_count": len(out),
        "records": out,
    }, f, indent=2)
print("saved → downloads/mdrfoi_diff/missing_report_numbers.json", flush=True)
print()
print("first 25 records:")
for r in out[:25]:
    mk = r["mdr_report_key"]
    rn = r["report_number"] or ""
    dr = r["date_received"] or ""
    et = r["event_type"] or ""
    mfr = r["manufacturer"] or ""
    print(f"  mdr={mk:14s}  report#={rn:30s}  date={dr:14s}  type={et:14s}  mfr={mfr[:40]}")
