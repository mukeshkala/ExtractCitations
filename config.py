"""Configuration for the Indian Kanoon Supreme Court scraper.

Edit this file to set default years and runtime behavior.
CLI arguments in scraper.py always override these defaults.
"""

from pathlib import Path

# -----------------------------
# Year selection defaults
# -----------------------------
START_YEAR = 1950
END_YEAR = 1950
YEAR_LIST = []  # Example: [1950, 1951, 1952]

# -----------------------------
# Output / runtime file paths
# -----------------------------
OUTPUT_DIR = Path("output")
OUTPUT_EXCEL = OUTPUT_DIR / "supreme_court_cases.xlsx"
FAILED_CASES_CSV = Path("failed_cases.csv")
PROGRESS_JSON = Path("progress.json")
LOG_FILE = Path("scraper.log")

# -----------------------------
# Request settings
# -----------------------------
REQUEST_DELAY_SECONDS = 1.5
RANDOMIZE_DELAY = True
REQUEST_DELAY_JITTER_SECONDS = 0.75
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
RETRY_BACKOFF_SECONDS = 2.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Optional additional headers to mimic a browser more closely.
ENABLE_EXTRA_HEADERS = True
EXTRA_HEADERS = {
    "Referer": "https://indiankanoon.org/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
}

# Optional browser cookie header copied from DevTools -> Network -> Request Headers.
# This can help the scraper reuse a browser-validated session when direct requests
# are blocked with 403 responses.
COOKIE_HEADER = ""

# Browser-backed fetching. The default below uses a visible Playwright Chromium
# session. Persistent profiles are supported, but the profile directory should
# stay browser-specific because an Edge-flavored profile can crash Chromium.
FETCH_MODE = "playwright"  # Supported: "requests", "playwright"
PLAYWRIGHT_HEADLESS = False
PLAYWRIGHT_NAVIGATION_TIMEOUT_MS = 30_000
PLAYWRIGHT_BROWSER_CHANNEL = ""  # Example: "", "chrome", "msedge"
PLAYWRIGHT_USE_PERSISTENT_CONTEXT = False
PLAYWRIGHT_PROFILE_DIR = Path(".playwright-chromium-profile")
PLAYWRIGHT_EDGE_PROFILE_DIR = Path(".playwright-edge-profile")
PLAYWRIGHT_FALLBACK_TO_NON_PERSISTENT = True
PLAYWRIGHT_LOCALE = "en-US"
PLAYWRIGHT_TIMEZONE_ID = "Asia/Kolkata"
PLAYWRIGHT_WAIT_FOR_MANUAL_OK_ON_BLOCK = True

# -----------------------------
# URLs
# -----------------------------
BASE_BROWSE_URL = "https://indiankanoon.org/browse/supremecourt/"

# Optional warm-up flow: make lightweight initial requests to seed cookies/session
# before year discovery and year-page crawling.
WARMUP_ENABLED = False
WARMUP_URLS = [
    "https://indiankanoon.org/",
    BASE_BROWSE_URL,
]
