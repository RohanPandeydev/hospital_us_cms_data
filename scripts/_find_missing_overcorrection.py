"""One-shot: locate the single 'overcorrection' MAUDE row that exists in
openFDA but not in our ClickHouse, and save its full payload."""

import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()
key = os.getenv("FDA_API_KEY")

q = ('product_problems:"overcorrection" '
     'AND date_received:[20100101 TO 20260504]')
r = requests.get("https://api.fda.gov/device/event.json",
                 params={"search": q, "limit": 1000, "api_key": key},
                 timeout=60)
r.raise_for_status()
fda_records = r.json().get("results", [])
print("openFDA records pulled:", len(fda_records))
fda_keys = {rec.get("mdr_report_key") for rec in fda_records}
fda_rns = {rec.get("report_number") for rec in fda_records}

import clickhouse_connect
c = clickhouse_connect.get_client(
    host=os.getenv("CLICKHOUSE_HOST"), port=443,
    username=os.getenv("CLICKHOUSE_USER"),
    password=os.getenv("CLICKHOUSE_PASSWORD"),
    database="default", secure=True,
)
res = c.query("""
    SELECT mdr_report_key, report_number
    FROM default.flattened_adverse_event
    WHERE product_problems ILIKE '%overcorrection%'
      AND date_received BETWEEN '20100101' AND '20260504'
""")
ch_rows = res.result_rows
ch_keys = {row[0] for row in ch_rows}
ch_rns = {row[1] for row in ch_rows}
print("CH rows:", len(ch_rows))

missing_keys = fda_keys - ch_keys
missing_rns = fda_rns - ch_rns
print("missing by mdr_report_key:", missing_keys)
print("missing by report_number :", missing_rns)

missing_records = [rec for rec in fda_records
                   if rec.get("mdr_report_key") in missing_keys
                   or rec.get("report_number") in missing_rns]

out_path = "downloads/maude_search/missing_in_clickhouse.json"
with open(out_path, "w") as f:
    json.dump({
        "product_problem": "overcorrection",
        "window": {"date_from": "20100101", "date_to": "20260504"},
        "fda_count": len(fda_records),
        "ch_count": len(ch_rows),
        "missing_count": len(missing_records),
        "missing_keys_mdr_report_key": sorted(missing_keys),
        "missing_keys_report_number": sorted(missing_rns),
        "missing_records": missing_records,
    }, f, indent=2, default=str)
print(f"saved {len(missing_records)} missing record(s) → {out_path}")

for m in missing_records:
    print()
    print("=== MISSING RECORD ===")
    print(" report_number :", m.get("report_number"))
    print(" mdr_report_key:", m.get("mdr_report_key"))
    print(" event_type    :", m.get("event_type"))
    print(" date_received :", m.get("date_received"))
    print(" date_of_event :", m.get("date_of_event"))
    print(" manufacturer  :", m.get("manufacturer_name"))
    print(" product_probs :", m.get("product_problems"))
    devs = m.get("device") or []
    if devs:
        d = devs[0]
        print(" device.brand_name        :", d.get("brand_name"))
        print(" device.generic_name      :", d.get("generic_name"))
        print(" device.manufacturer_d_name:", d.get("manufacturer_d_name"))
        print(" device.product_code      :", d.get("device_report_product_code"))
