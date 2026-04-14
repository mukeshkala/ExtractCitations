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
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
RETRY_BACKOFF_SECONDS = 2.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# -----------------------------
# URLs
# -----------------------------
BASE_BROWSE_URL = "https://indiankanoon.org/browse/supremecourt/"
