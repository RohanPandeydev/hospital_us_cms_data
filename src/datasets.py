"""Registry of CMS Provider Data datasets to ingest.

kind:
  - 'hospital_master'   -> cms_hospitals table (xubh-q36u only)
  - 'facility_measure'  -> cms_hospital_measures (per-hospital measures)
  - 'state_measure'     -> cms_state_measures
  - 'national_measure'  -> cms_national_measures
"""

DATASETS = [
    {
        "id": "xubh-q36u",
        "name": "Hospital General Information",
        "kind": "hospital_master",
    },
    {
        "id": "ynj2-r877",
        "name": "Complications and Deaths - Hospital",
        "kind": "facility_measure",
    },
    {
        "id": "632h-zaca",
        "name": "Unplanned Hospital Visits - Hospital",
        "kind": "facility_measure",
    },
    {
        "id": "9n3s-kdb3",
        "name": "Hospital Readmissions Reduction Program (HRRP)",
        "kind": "facility_measure",
    },
    {
        "id": "77hc-ibv8",
        "name": "Healthcare Associated Infections - Hospital",
        "kind": "facility_measure",
    },
    {
        "id": "muwa-iene",
        "name": "CMS Medicare PSI-90 and component measures",
        "kind": "facility_measure",
    },
    {
        "id": "dgck-syfz",
        "name": "Patient Survey (HCAHPS) - Hospital",
        "kind": "facility_measure",
    },
    {
        "id": "bs2r-24vh",
        "name": "Complications and Deaths - State",
        "kind": "state_measure",
    },
    {
        "id": "qqw3-t4ie",
        "name": "Complications and Deaths - National",
        "kind": "national_measure",
    },
    # --- Device-linkage additions ---
    {
        "id": "tqkv-mgxq",
        "name": "Comprehensive Care for Joint Replacement (CJR)",
        "kind": "wide_facility",
    },
    {
        "id": "4jcv-atw7",
        "name": "Ambulatory Surgical Center Quality Measures - Facility",
        "kind": "wide_facility",
    },
    {
        "id": "48nr-hqxx",
        "name": "ASC OAS CAHPS Survey - Facility",
        "kind": "wide_facility",
    },
    {
        "id": "y9us-9xdf",
        "name": "Footnote Crosswalk (reference)",
        "kind": "footnote_crosswalk",
    },
    # --- Priority 2: Timely & Effective Care, HACRP, MSPB, VBP, Cancer, HAI/HCAHPS bench ---
    {"id": "yv7e-xc69", "name": "Timely and Effective Care - Hospital",           "kind": "facility_measure"},
    {"id": "apyc-v239", "name": "Timely and Effective Care - State",              "kind": "state_measure"},
    {"id": "isrn-hqyy", "name": "Timely and Effective Care - National",           "kind": "national_measure"},
    {"id": "yq43-i98g", "name": "Hospital-Acquired Condition Reduction Program",  "kind": "facility_measure"},
    {"id": "rrqw-56er", "name": "Medicare Spending Per Beneficiary - Hospital",   "kind": "facility_measure"},
    {"id": "rs6n-9qwg", "name": "Medicare Spending Per Beneficiary - State",      "kind": "state_measure"},
    {"id": "3n5g-6b7f", "name": "Medicare Spending Per Beneficiary - National",   "kind": "national_measure"},
    {"id": "z8ax-x9j1", "name": "PPS-Exempt Cancer Hospital Quality",             "kind": "facility_measure"},
    {"id": "ypbt-wvdk", "name": "Hospital VBP Total Performance Score",           "kind": "facility_measure"},
    {"id": "nrdb-3fcy", "name": "Maternal Health - Hospital",                     "kind": "facility_measure"},
    {"id": "k2ze-bqvw", "name": "HAI - State Benchmark",                          "kind": "state_measure"},
    {"id": "yd3s-jyhd", "name": "HAI - National Benchmark",                       "kind": "national_measure"},
    {"id": "84jm-wiui", "name": "HCAHPS - State Benchmark",                       "kind": "state_measure"},
    {"id": "99ue-w85f", "name": "HCAHPS - National Benchmark",                    "kind": "national_measure"},
    # --- Phase 3: FDA openFDA ---
    {
        "id": "fda_maude_events",
        "name": "FDA MAUDE — Device Adverse Events",
        "kind": "fda_events",
        "source": "openfda",
    },
    {
        "id": "fda_gudid",
        "name": "FDA GUDID — Unique Device Identifier Database",
        "kind": "fda_gudid",
        "source": "openfda_udi",
    },
    # --- Phase 2: CMS data-api/v1 (Medicare utilization, DMEPOS) ---
    # data-api/v1 utilization datasets. Full-table scans time out server-side;
    # iterate by state (filter_field) and pull only the columns we actually
    # upsert (columns) — dramatically smaller payload + reliable pagination.
    # See docs: /data-api/v1/dataset/{uuid}/data?filter[FIELD]=VAL&column=a,b,c
    {
        "id": "medicare_inpatient_by_provider",
        "uuid": "ee6fb1a5-39b9-46b3-a980-a7284551a732",
        "name": "Medicare Inpatient Hospitals - by Provider",
        "kind": "cms_summary",
        "source": "cms_data_api",
        "filter_field": "Rndrng_Prvdr_State_Abrvtn",
        "columns": [
            "Rndrng_Prvdr_CCN", "Rndrng_Prvdr_Org_Name",
            "Rndrng_Prvdr_State_Abrvtn", "Rndrng_Prvdr_City",
            "Tot_Dschrgs", "Tot_Benes",
            "Avg_Tot_Pymt_Amt", "Avg_Mdcr_Pymt_Amt",
        ],
    },
    {
        "id": "medicare_inpatient_by_provider_service",
        "uuid": "690ddc6c-2767-4618-b277-420ffb2bf27c",
        "name": "Medicare Inpatient Hospitals - by Provider and Service (MS-DRG)",
        "kind": "cms_summary",
        "source": "cms_data_api",
        "filter_field": "Rndrng_Prvdr_State_Abrvtn",
        "columns": [
            "Rndrng_Prvdr_CCN", "Rndrng_Prvdr_Org_Name",
            "Rndrng_Prvdr_State_Abrvtn", "Rndrng_Prvdr_City",
            "DRG_Cd", "DRG_Desc",
            "Tot_Dschrgs", "Avg_Tot_Pymt_Amt", "Avg_Mdcr_Pymt_Amt",
        ],
    },
    {
        "id": "medicare_outpatient_by_provider_service",
        "uuid": "ccbc9a44-40d4-46b4-a709-5caa59212e50",
        "name": "Medicare Outpatient Hospitals - by Provider and Service (APC)",
        "kind": "cms_summary",
        "source": "cms_data_api",
        "filter_field": "Rndrng_Prvdr_State_Abrvtn",
        "columns": [
            "Rndrng_Prvdr_CCN", "Rndrng_Prvdr_Org_Name",
            "Rndrng_Prvdr_State_Abrvtn", "Rndrng_Prvdr_City",
            "HCPCS_Cd", "HCPCS_Desc",
            "Bene_Cnt", "Srvcs",
            "Avg_Tot_Sbmtd_Chrg", "Avg_Mdcr_Pymt_Amt",
        ],
    },
    {
        "id": "medicare_dmepos_by_supplier",
        "uuid": "a2d56d3f-3531-4315-9d87-e29986516b41",
        "name": "Medicare DMEPOS - by Supplier",
        "kind": "cms_summary",
        "source": "cms_data_api",
        "filter_field": "Rfrg_Prvdr_State_Abrvtn",
    },
    {
        "id": "medicare_dmepos_by_supplier_service",
        "uuid": "1746a83e-bb65-4300-8e02-21edbab77c6b",
        "name": "Medicare DMEPOS - by Supplier and Service (HCPCS device billing)",
        "kind": "cms_summary",
        "source": "cms_data_api",
        "filter_field": "Rfrg_Prvdr_State_Abrvtn",
        "columns": [
            "Rfrg_NPI", "Rfrg_Prvdr_Last_Org_Name", "Rfrg_Prvdr_First_Name",
            "Rfrg_Prvdr_State_Abrvtn",
            "HCPCS_Cd", "HCPCS_Desc",
            "Tot_Suplrs", "Tot_Suplr_Benes", "Tot_Suplr_Srvcs",
            "Avg_Suplr_Mdcr_Pymt_Amt",
        ],
    },
    {
        "id": "medicare_dmepos_by_referring_provider",
        "uuid": "f8603e5b-9c47-4c52-9b47-a4ef92dfada4",
        "name": "Medicare DMEPOS - by Referring Provider",
        "kind": "cms_summary",
        "source": "cms_data_api",
        "filter_field": "Rfrg_Prvdr_State_Abrvtn",
    },
    {
        "id": "medicare_physician_by_provider",
        "uuid": "8889d81e-2ee7-448f-8713-f071038289b5",
        "name": "Medicare Physician & Other Practitioners - by Provider",
        "kind": "cms_summary",
        "source": "cms_data_api",
        "filter_field": "Rndrng_Prvdr_State_Abrvtn",
    },
    {
        "id": "medicare_physician_by_provider_service",
        "uuid": "92396110-2aed-4d63-a6a2-5d6207d46a29",
        "name": "Medicare Physician & Other Practitioners - by Provider and Service",
        "kind": "cms_summary",
        "source": "cms_data_api",
        "filter_field": "Rndrng_Prvdr_State_Abrvtn",
        "columns": [
            "Rndrng_NPI", "Rndrng_Prvdr_Last_Org_Name",
            "Rndrng_Prvdr_State_Abrvtn", "Rndrng_Prvdr_City",
            "HCPCS_Cd", "HCPCS_Desc",
            "Tot_Benes", "Tot_Srvcs",
            "Avg_Sbmtd_Chrg", "Avg_Mdcr_Pymt_Amt",
        ],
    },
    # --- Reference / master tables ---
    {
        "id": "provider_of_services",
        "uuid": "086e48c4-87a6-4be1-8823-29e8da8f225b",
        "name": "Provider of Services (POS) File — IQIES",
        "kind": "cms_summary",
        "source": "cms_data_api",
    },
    {
        "id": "betos_classification",
        "uuid": "e3db6e56-149f-49ce-b374-40aecda2357b",
        "name": "Restructured BETOS HCPCS Classification",
        "kind": "cms_summary",
        "source": "cms_data_api",
    },
    {
        "id": "hospital_cost_report",
        "uuid": "44060663-47d8-4ced-a115-b53b4c270acb",
        "name": "Hospital Provider Cost Report (HCRIS)",
        "kind": "cms_summary",
        "source": "cms_data_api",
    },
    {
        "id": "hcpcs_master",
        "name": "HCPCS Level II Master (quarterly ZIP)",
        "kind": "hcpcs_master",
        "source": "cms_hcpcs_static",
    },
    # --- Priority 3: Open Payments (manufacturer → hospital/physician $) ---
    # Same DKAN API as CMS provider-data but different host: openpaymentsdata.cms.gov
    {
        "id": "op_2024_general",
        "uuid": "e6b17c6a-2534-4207-a4a1-6746a14911ff",
        "name": "Open Payments 2024 - General Payment Data",
        "kind": "open_payments",
        "source": "open_payments",
    },
    {
        "id": "op_2024_research",
        "uuid": "2f15cb85-8887-4dcc-a318-1f8ec1d815b3",
        "name": "Open Payments 2024 - Research Payment Data",
        "kind": "open_payments",
        "source": "open_payments",
    },
    {
        "id": "op_2024_ownership",
        "uuid": "9ac4f7f8-b6e4-4d80-8410-4aba7e71dd02",
        "name": "Open Payments 2024 - Ownership Payment Data",
        "kind": "open_payments",
        "source": "open_payments",
    },
]


def get_dataset_by_uuid(uuid):
    for ds in DATASETS:
        if ds.get("uuid") == uuid:
            return ds
    return None


def get_dataset(dataset_id):
    for ds in DATASETS:
        if ds["id"] == dataset_id:
            return ds
    return None
