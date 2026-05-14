# New Sources — Quick Reference

12 new sources ingested. For each: the link + what it holds for us.

---

### 1. OIG LEIE — Provider Exclusions

**Link:** https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv

**Holds:** List of 83,162 providers and entities banned from Medicare, Medicaid, and every federally funded healthcare program. Each row has NPI, name, exclusion reason (fraud, license revocation, patient abuse, etc.), and exclusion date.

**For us:** Compliance red-flag layer. Any NPI in our pipeline that appears here is uninvestable. Found 105 NPIs with both BSc/industry payments AND an active OIG exclusion.

---

### 2. Medicare Physician Fee Schedule (MPFS) — RVU & Payment File

**Link:** https://www.cms.gov/files/zip/rvu25d-updated.zip (quarterly)

**Holds:** 19,090 HCPCS/CPT codes with per-code reimbursement in two settings: office (non-facility) and hospital (facility), plus Work RVU, PE RVU, malpractice RVU, conversion factor.

**For us:** Office-setting payment $ — complements our existing OPPS Addendum B (hospital-setting). Tells us where each procedure is more profitable. Example: code 75705 (artery x-ray, spine) pays $166.78 in office.

---

### 3. Medicare Opt-Out Affidavits

**Link:** https://data.cms.gov → "Opt Out Affidavits" → CSV

**Holds:** 55,537 providers who formally opted out of Medicare. Cannot bill Medicare for ANY service.

**For us:** Negative filter for sales targeting. Manufacturers cannot drive Medicare revenue through these providers — drop them from any prospect list.

---

### 4. Provider of Services (POS) File

**Link:** https://data.cms.gov → "Provider of Services File - Quality Improvement and Evaluation System" → Hospital_and_other.DATA.QX_YYYY.csv

**Holds:** 44,429 Medicare-certified facilities — hospitals, ASCs, SNFs, dialysis, hospices, FQHCs, RHCs, transplant centers. With CCN, type, address, beds, certification dates.

**For us:** Full addressable facility universe. Existing `cms_hospitals` had ~5K hospitals only — POS adds 5,756 ASCs (Watchman-eligible), plus SNFs and dialysis. 6× expansion.

---

### 5. Order & Referring NPI

**Link:** https://data.cms.gov → "Order and Referring" → OrderReferring_YYYYMMDD.csv

**Holds:** 1,998,434 NPIs with flags for which Medicare services they're eligible to order/refer: Part B, DME, HHA, Power Mobility, Hospice.

**For us:** Eligibility filter. Tells us if an NPI can legally order/refer a device or supply through Medicare. Lite NPPES substitute.

---

### 6. Medicare Provider/Supplier Taxonomy Crosswalk

**Link:** https://data.cms.gov → "Medicare Provider and Supplier Taxonomy Crosswalk" → CSV

**Holds:** 557 rows mapping CMS specialty codes ↔ NUCC taxonomy codes (e.g., specialty code 06 = Cardiology = taxonomy 207RC0000X).

**For us:** Filter NPIs by clinical specialty. Lets us say "show me only interventional cardiologists" or "only neurosurgeons" when targeting BSc devices.

---

### 7. Medicare FFS Public Provider Enrollment

**Link:** https://data.cms.gov → "Medicare Fee-For-Service Public Provider Enrollment" → PPEF_Enrollment_Extract.csv

**Holds:** 2,613,371 NPI + provider-type-code rows. Every active Medicare FFS enrollment with state, name/org, type description.

**For us:** Definitive "who can currently bill Medicare" list. If an NPI isn't here, they're not a viable sales target for Medicare-reimbursed devices.

---

### 8. HCRIS — Hospital Provider Cost Report

**Link:** https://data.cms.gov → "Hospital Provider Cost Report" → CostReport_YYYY_Final.csv

**Holds:** 6,103 hospital cost reports for fiscal year 2023. Per hospital: beds, discharges (Medicare, Medicaid, total), total charges, total costs, capital expenditure, uncompensated care cost.

**For us:** Market sizing per hospital. Top hospital by charges: CCN 330214 = $45,115M charges, $113.77M uncompensated, 1,609 beds. Filter financially-stressed hospitals out of early-launch targeting.

---

### 9. Medicare Revalidation Due Date List

**Link:** https://data.cms.gov → "Revalidation Due Date List" → revalidation_base.csv

**Holds:** 2,790,243 enrolled providers with their next revalidation deadline.

**For us:** Lapse-risk early warning. A provider with a missed revalidation loses billing privileges — meaning their procedure volume drops to zero. Use to flag providers due in next 90 days.

---

### 10. Medicare Monthly Enrollment by State

**Link:** https://data.cms.gov → "Medicare Monthly Enrollment" → Medicare Monthly Enrollment Data_MonthYYYY.csv

**Holds:** 9,106 state-level monthly enrollment rows. Total beneficiaries split into Original Medicare vs. Medicare Advantage, by state and month.

**For us:** Hospital catchment market sizing. Tells us how many Medicare beneficiaries are in a given state — defines the addressable patient pool.

---

### 11. Medicare Part C/D Star Ratings

**Link:** https://www.cms.gov/files/zip/2026-star-ratings-data-tables.zip (annual)

**Holds:** 4,674 per-contract summary star ratings (2025 + 2026) — Part C summary, Part D summary, Overall rating.

**For us:** MA plan quality signal. Useful when assessing which MA contracts are growing (high-star plans) — those contracts drive hospital network growth and indirectly device adoption.

---

### 12. BETOS (Restructured BETOS Classification System)

**Link:** https://data.cms.gov → "Restructured BETOS Classification System" → RBCS Taxonomy_RYYYYY.csv

**Holds:** 16,618 HCPCS/CPT codes mapped to clinical categories: cardiovascular, neuro, ortho, imaging, etc. — with category, subcategory, family.

**For us:** Filter HCPCS by clinical lens. Lets us say "all device-implant codes" in one query, instead of guessing which codes are device-relevant from their text descriptions.
