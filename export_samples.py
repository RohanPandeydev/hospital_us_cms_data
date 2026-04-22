#!/usr/bin/env python3
"""Build CSV exports (~10k rows total) for manual inspection.

Writes into exports/ — one file per layer, plus a combined join so you can
see the end-to-end CMS↔FDA linkage.
"""

import csv
import os
from src import db

OUT = os.path.join(os.path.dirname(__file__), "exports")
os.makedirs(OUT, exist_ok=True)


def write_csv(name, header, rows):
    path = os.path.join(OUT, name)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  wrote {name:<40} {len(rows):>7,} rows")
    return len(rows)


def export_bridge(conn):
    """Bridge table + HCPCS description + MAUDE event count for each product_code."""
    sql = """
    SELECT b.hcpcs_code,
           h.short_desc,
           h.is_device,
           b.product_code,
           b.device_category,
           b.match_method,
           b.confidence,
           COALESCE(e.n_events, 0) AS maude_event_count,
           COALESCE(g.n_gudid,  0) AS gudid_device_count,
           b.source_notes
      FROM bridge_hcpcs_to_product_code b
      LEFT JOIN hcpcs_master h ON h.hcpcs_code = b.hcpcs_code
      LEFT JOIN (
          SELECT product_code, count(DISTINCT report_number) AS n_events
            FROM fda_maude_devices GROUP BY product_code
      ) e ON e.product_code = b.product_code
      LEFT JOIN (
          SELECT product_code, count(*) AS n_gudid
            FROM fda_gudid_devices GROUP BY product_code
      ) g ON g.product_code = b.product_code
     ORDER BY b.hcpcs_code, b.product_code;
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    header = ["hcpcs_code","hcpcs_short_desc","hcpcs_is_device","product_code",
              "device_category","match_method","confidence",
              "maude_event_count","gudid_device_count","source_notes"]
    return write_csv("01_bridge_hcpcs_to_product_code.csv", header, rows)


def export_hospitals(conn):
    sql = """
    SELECT facility_id, facility_name, city, state, zip_code,
           hospital_type, hospital_ownership, emergency_services, overall_rating
      FROM cms_hospitals
     ORDER BY state, facility_id;
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    header = ["facility_id","facility_name","city","state","zip_code",
              "hospital_type","hospital_ownership","emergency_services","overall_rating"]
    return write_csv("02_cms_hospitals.csv", header, rows)


def export_hospital_measures(conn):
    """A 2k-row sample focused on device-relevant measures (PSI/HAI/HRRP/COMP)."""
    sql = """
    SELECT m.facility_id, h.facility_name, h.state,
           m.dataset_id, m.measure_id, m.measure_name,
           m.score, m.compared_to_national, m.denominator,
           m.start_date, m.end_date
      FROM cms_hospital_measures m
      JOIN cms_hospitals h ON h.facility_id = m.facility_id
     WHERE m.measure_id ~* '(COMP_HIP_KNEE|MORT_30|READM_30|PSI_|HAI_|EDAC_)'
     ORDER BY m.facility_id, m.measure_id
     LIMIT 2000;
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    header = ["facility_id","facility_name","state","dataset_id","measure_id",
              "measure_name","score","compared_to_national","denominator",
              "start_date","end_date"]
    return write_csv("03_cms_measures_device_relevant.csv", header, rows)


def export_maude_linked(conn):
    """MAUDE events where product_code is in the bridge — with HCPCS attached."""
    sql = """
    SELECT DISTINCT ON (e.report_number, d.product_code, b.hcpcs_code)
           e.report_number, e.event_type, e.date_of_event, e.date_received,
           e.manufacturer_name, e.facility_state, e.patient_outcomes,
           e.device_problems,
           d.product_code, d.brand_name, d.generic_name,
           b.hcpcs_code, h.short_desc AS hcpcs_desc,
           b.device_category, b.confidence AS bridge_confidence
      FROM fda_maude_events e
      JOIN fda_maude_devices d ON d.report_number = e.report_number
      JOIN bridge_hcpcs_to_product_code b ON b.product_code = d.product_code
      LEFT JOIN hcpcs_master h ON h.hcpcs_code = b.hcpcs_code
     ORDER BY e.report_number, d.product_code, b.hcpcs_code
     LIMIT 2500;
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    header = ["report_number","event_type","date_of_event","date_received",
              "manufacturer_name","facility_state","patient_outcomes","device_problems",
              "product_code","brand_name","generic_name",
              "hcpcs_code","hcpcs_desc","device_category","bridge_confidence"]
    return write_csv("04_maude_events_linked_to_hcpcs.csv", header, rows)


def export_gudid_linked(conn):
    sql = """
    SELECT g.primary_di, g.product_code, g.brand_name, g.company_name,
           left(g.device_description, 160) AS device_description,
           g.gmdn_pt_name,
           b.hcpcs_code, h.short_desc AS hcpcs_desc,
           b.device_category, b.match_method, b.confidence
      FROM fda_gudid_devices g
      JOIN bridge_hcpcs_to_product_code b ON b.product_code = g.product_code
      LEFT JOIN hcpcs_master h ON h.hcpcs_code = b.hcpcs_code
     ORDER BY b.hcpcs_code, g.primary_di
     LIMIT 2500;
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    header = ["primary_di","product_code","brand_name","company_name",
              "device_description","gmdn_pt_name",
              "hcpcs_code","hcpcs_desc","device_category","match_method","confidence"]
    return write_csv("05_gudid_linked_to_hcpcs.csv", header, rows)


def export_combined(conn):
    """End-to-end: MAUDE event × device × bridge × HCPCS × (CMS facility via state)."""
    sql = """
    SELECT e.report_number,
           e.date_of_event,
           e.event_type,
           e.manufacturer_name,
           e.facility_state,
           e.patient_outcomes,
           d.product_code,
           d.brand_name,
           b.hcpcs_code,
           h.short_desc AS hcpcs_desc,
           b.device_category,
           b.confidence  AS bridge_confidence,
           (SELECT count(*) FROM cms_hospitals ch
              WHERE ch.state = e.facility_state) AS n_cms_hosp_in_state,
           (SELECT count(*) FROM cms_hospital_measures m
              WHERE m.measure_id ~* 'PSI_|HAI_|READM_30|COMP_HIP_KNEE'
                AND m.facility_id IN (
                    SELECT facility_id FROM cms_hospitals WHERE state = e.facility_state
                )) AS n_cms_measures_in_state
      FROM fda_maude_events e
      JOIN fda_maude_devices d ON d.report_number = e.report_number
      JOIN bridge_hcpcs_to_product_code b ON b.product_code = d.product_code
      LEFT JOIN hcpcs_master h ON h.hcpcs_code = b.hcpcs_code
     WHERE e.facility_state IS NOT NULL
     ORDER BY e.facility_state, e.date_of_event DESC
     LIMIT 2000;
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    header = ["report_number","date_of_event","event_type","manufacturer_name",
              "facility_state","patient_outcomes",
              "product_code","brand_name","hcpcs_code","hcpcs_desc",
              "device_category","bridge_confidence",
              "n_cms_hosp_in_state","n_cms_measures_in_state"]
    return write_csv("06_combined_fda_event_to_cms_state.csv", header, rows)


def main():
    total = 0
    with db.connect() as conn:
        print(f"Writing to {OUT}")
        total += export_bridge(conn)
        total += export_hospitals(conn)
        total += export_hospital_measures(conn)
        total += export_maude_linked(conn)
        total += export_gudid_linked(conn)
        total += export_combined(conn)
    print(f"\nTotal rows exported: {total:,}")


if __name__ == "__main__":
    main()
