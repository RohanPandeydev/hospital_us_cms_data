import os
from dotenv import load_dotenv

load_dotenv()

# ---- ClickHouse (primary warehouse) ----
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "localhost")
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT", "443"))
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")
CLICKHOUSE_DATABASE = os.getenv("CLICKHOUSE_DATABASE", "default")
CLICKHOUSE_SECURE = os.getenv("CLICKHOUSE_SECURE", "true").lower() in ("1", "true", "yes")

# ---- CMS / FDA upstream APIs ----
CMS_API_BASE = os.getenv(
    "CMS_API_BASE",
    "https://data.cms.gov/provider-data/api/1/datastore/query",
)
CMS_PAGE_SIZE = int(os.getenv("CMS_PAGE_SIZE", "500"))
CMS_HTTP_TIMEOUT = int(os.getenv("CMS_HTTP_TIMEOUT", "60"))
CMS_MAX_RETRIES = int(os.getenv("CMS_MAX_RETRIES", "5"))

FDA_API_BASE = os.getenv("FDA_API_BASE", "https://api.fda.gov")
FDA_API_KEY = os.getenv("FDA_API_KEY", "")
FDA_PAGE_SIZE = int(os.getenv("FDA_PAGE_SIZE", "1000"))
FDA_HTTP_TIMEOUT = int(os.getenv("FDA_HTTP_TIMEOUT", "60"))
FDA_MAX_RETRIES = int(os.getenv("FDA_MAX_RETRIES", "5"))

# ---- TinyFish AI (web-extraction agent) ----
# Used for scraping unstructured sources we don't reach via FDA/CMS APIs —
# e.g. EUDAMED (EU medical device database), FDA 483 inspection letters,
# state hospital quality registries. Streaming SSE endpoint.
TINYFISH_API_KEY = os.getenv("TINYFISH_API_KEY", "")
TINYFISH_BASE_URL = os.getenv("TINYFISH_BASE_URL", "https://agent.tinyfish.ai")
TINYFISH_TIMEOUT = int(os.getenv("TINYFISH_TIMEOUT", "600"))
