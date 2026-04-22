import os
from dotenv import load_dotenv

load_dotenv()

PGHOST = os.getenv("PGHOST", "localhost")
PGPORT = int(os.getenv("PGPORT", "5432"))
PGUSER = os.getenv("PGUSER", "postgres")
PGPASSWORD = os.getenv("PGPASSWORD", "")
PGDATABASE = os.getenv("PGDATABASE", "cms_hospitals")

CMS_API_BASE = os.getenv(
    "CMS_API_BASE",
    "https://data.cms.gov/provider-data/api/1/datastore/query",
)
CMS_PAGE_SIZE = int(os.getenv("CMS_PAGE_SIZE", "500"))
CMS_HTTP_TIMEOUT = int(os.getenv("CMS_HTTP_TIMEOUT", "60"))
CMS_MAX_RETRIES = int(os.getenv("CMS_MAX_RETRIES", "5"))

FDA_API_BASE = os.getenv("FDA_API_BASE", "https://api.fda.gov")
FDA_API_KEY = os.getenv("FDA_API_KEY", "")
FDA_PAGE_SIZE = int(os.getenv("FDA_PAGE_SIZE", "1000"))   # openFDA max 1000
FDA_HTTP_TIMEOUT = int(os.getenv("FDA_HTTP_TIMEOUT", "60"))
FDA_MAX_RETRIES = int(os.getenv("FDA_MAX_RETRIES", "5"))


def pg_dsn(database=None):
    db = database if database is not None else PGDATABASE
    parts = [
        f"host={PGHOST}",
        f"port={PGPORT}",
        f"user={PGUSER}",
        f"dbname={db}",
    ]
    if PGPASSWORD:
        parts.append(f"password={PGPASSWORD}")
    return " ".join(parts)
