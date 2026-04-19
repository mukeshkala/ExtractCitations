"""Production-oriented scraper for Indian Kanoon Supreme Court case metadata.

Features:
- Batch scraping by explicit year list or year range
- Resume support with progress checkpointing
- Failed case retry mode
- Exponential backoff for HTTP retries
- Incremental Excel writing with duplicate prevention
- Console + file logging

Usage examples:
    python scraper.py --years 1950 1951 1952
    python scraper.py --start-year 1950 --end-year 1955
    python scraper.py --resume
    python scraper.py --retry-failed
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from openpyxl import load_workbook

import config

EXCEL_COLUMNS = [
    "year",
    "cites_block",
    "court",
    "title",
    "equivalent_citations",
    "author",
    "bench",
    "case_url",
    "scraped_at",
    "status",
]


def setup_logging() -> None:
    """Configure console + file logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def now_iso() -> str:
    """Current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


class SupremeCourtScraper:
    """Scraper class that manages state, requests, parsing, and persistence."""

    def __init__(self) -> None:
        self.fetch_mode = str(getattr(config, "FETCH_MODE", "requests")).strip().lower() or "requests"
        self.session = requests.Session()
        self.session.headers.update(self.build_default_headers())
        self.apply_configured_cookie_header()
        self.playwright_manager: Optional[Any] = None
        self.playwright_browser: Optional[Any] = None
        self.playwright_context: Optional[Any] = None
        self.playwright_page: Optional[Any] = None

        self.progress = self.load_progress()
        self.processed_case_urls: Set[str] = set(self.progress.get("processed_case_urls", []))
        self.failed_case_urls: Set[str] = set(self.progress.get("failed_case_urls", []))
        self.excel_case_urls: Set[str] = self.load_existing_case_urls_from_excel()

        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self.ensure_failed_cases_csv_exists()

    @staticmethod
    def build_default_headers() -> Dict[str, str]:
        """Build base headers, with optional extra browser-like headers."""
        headers = {
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
        if config.ENABLE_EXTRA_HEADERS:
            headers.update(config.EXTRA_HEADERS)
        return headers

    def apply_configured_cookie_header(self) -> None:
        """Optionally reuse a browser cookie header copied from DevTools."""
        cookie_header = str(getattr(config, "COOKIE_HEADER", "") or "").strip()
        if not cookie_header:
            return

        self.session.headers["Cookie"] = cookie_header
        logging.info("Applied configured browser cookie header to session")

    def initialize_fetcher(self) -> None:
        """Initialize optional browser-backed fetch machinery."""
        if self.fetch_mode == "requests":
            logging.info("Fetch mode: requests")
            return

        if self.fetch_mode != "playwright":
            raise ValueError(f"Unsupported fetch mode: {self.fetch_mode}")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright fetch mode requires the 'playwright' package. "
                "Install dependencies with 'pip install -r requirements.txt' and run "
                "'python -m playwright install chromium'."
            ) from exc

        self.playwright_manager = sync_playwright().start()
        extra_headers = {
            key: value
            for key, value in self.session.headers.items()
            if key.lower() not in {"host", "content-length"}
        }
        launch_kwargs = {
            "headless": bool(getattr(config, "PLAYWRIGHT_HEADLESS", True)),
        }
        browser_channel = str(getattr(config, "PLAYWRIGHT_BROWSER_CHANNEL", "") or "").strip()
        if browser_channel:
            launch_kwargs["channel"] = browser_channel

        if bool(getattr(config, "PLAYWRIGHT_USE_PERSISTENT_CONTEXT", False)):
            try:
                self.playwright_context = self.launch_persistent_context(launch_kwargs, extra_headers, browser_channel)
                self.playwright_browser = self.playwright_context.browser
            except Exception as exc:  # pylint: disable=broad-except
                if not bool(getattr(config, "PLAYWRIGHT_FALLBACK_TO_NON_PERSISTENT", True)):
                    raise
                logging.warning(
                    "Persistent Playwright context failed (%s). Falling back to a fresh non-persistent browser session.",
                    exc,
                )
                self.playwright_browser = self.playwright_manager.chromium.launch(**launch_kwargs)
                self.playwright_context = self.playwright_browser.new_context(
                    user_agent=self.session.headers.get("User-Agent", config.USER_AGENT),
                    extra_http_headers=extra_headers,
                    locale=str(getattr(config, "PLAYWRIGHT_LOCALE", "en-US")),
                    timezone_id=str(getattr(config, "PLAYWRIGHT_TIMEZONE_ID", "Asia/Kolkata")),
                    viewport={"width": 1440, "height": 900},
                )
        else:
            self.playwright_browser = self.playwright_manager.chromium.launch(**launch_kwargs)
            self.playwright_context = self.playwright_browser.new_context(
                user_agent=self.session.headers.get("User-Agent", config.USER_AGENT),
                extra_http_headers=extra_headers,
                locale=str(getattr(config, "PLAYWRIGHT_LOCALE", "en-US")),
                timezone_id=str(getattr(config, "PLAYWRIGHT_TIMEZONE_ID", "Asia/Kolkata")),
                viewport={"width": 1440, "height": 900},
            )

        self.playwright_context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = window.chrome || { runtime: {} };
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4]});
            """
        )
        self.playwright_page = self.playwright_context.new_page()
        self.playwright_page.set_default_navigation_timeout(
            int(getattr(config, "PLAYWRIGHT_NAVIGATION_TIMEOUT_MS", 30_000))
        )
        logging.info(
            "Fetch mode: playwright | headless=%s | channel=%s | persistent=%s",
            bool(getattr(config, "PLAYWRIGHT_HEADLESS", True)),
            browser_channel or "chromium",
            bool(getattr(config, "PLAYWRIGHT_USE_PERSISTENT_CONTEXT", True)),
        )

    def launch_persistent_context(
        self,
        launch_kwargs: Dict[str, Any],
        extra_headers: Dict[str, str],
        browser_channel: str,
    ) -> Any:
        """Launch a persistent browser context with a browser-specific profile path."""
        if browser_channel == "msedge":
            profile_dir = Path(getattr(config, "PLAYWRIGHT_EDGE_PROFILE_DIR", Path(".playwright-edge-profile")))
        else:
            profile_dir = Path(getattr(config, "PLAYWRIGHT_PROFILE_DIR", Path(".playwright-chromium-profile")))

        profile_dir.mkdir(parents=True, exist_ok=True)
        return self.playwright_manager.chromium.launch_persistent_context(
            str(profile_dir),
            user_agent=self.session.headers.get("User-Agent", config.USER_AGENT),
            extra_http_headers=extra_headers,
            locale=str(getattr(config, "PLAYWRIGHT_LOCALE", "en-US")),
            timezone_id=str(getattr(config, "PLAYWRIGHT_TIMEZONE_ID", "Asia/Kolkata")),
            viewport={"width": 1440, "height": 900},
            **launch_kwargs,
        )

    def close(self) -> None:
        """Release browser resources when Playwright mode is used."""
        if self.playwright_page is not None:
            self.playwright_page.close()
            self.playwright_page = None
        if self.playwright_context is not None:
            self.playwright_context.close()
            self.playwright_context = None
        if self.playwright_browser is not None:
            self.playwright_browser.close()
            self.playwright_browser = None
        if self.playwright_manager is not None:
            self.playwright_manager.stop()
            self.playwright_manager = None

    @staticmethod
    def sleep_request_delay() -> None:
        """Sleep between requests, with optional random jitter."""
        delay = config.REQUEST_DELAY_SECONDS
        if config.RANDOMIZE_DELAY:
            jitter = max(0.0, config.REQUEST_DELAY_JITTER_SECONDS)
            delay += random.uniform(-jitter, jitter)
            delay = max(0.0, delay)
        time.sleep(delay)

    def warmup_session(self) -> None:
        """Run optional warm-up requests to seed session cookies and server context."""
        if not config.WARMUP_ENABLED:
            return

        urls = [u for u in config.WARMUP_URLS if str(u).strip()]
        if not urls:
            return

        logging.info("Starting warm-up flow with %s URL(s)", len(urls))
        for i, url in enumerate(urls, start=1):
            try:
                response = self.fetch_url(url)
                logging.info(
                    "Warm-up request %s/%s: %s -> HTTP %s",
                    i,
                    len(urls),
                    url,
                    response.status_code,
                )
            except Exception as exc:  # pylint: disable=broad-except
                logging.warning("Warm-up request failed: %s | %s", url, exc)
            self.sleep_request_delay()

    # -----------------------------
    # HTTP helpers
    # -----------------------------
    def fetch_url(self, url: str) -> requests.Response:
        """Fetch a URL using either requests or Playwright."""
        if self.fetch_mode == "playwright":
            return self.fetch_url_with_playwright(url)
        return self.session.get(url, timeout=config.REQUEST_TIMEOUT)

    def fetch_url_with_playwright(self, url: str) -> requests.Response:
        """Load a page in Chromium and convert it to a Response-like object."""
        if self.playwright_page is None:
            raise RuntimeError("Playwright page is not initialized")

        response = self.playwright_page.goto(url, wait_until="domcontentloaded")
        if response is None:
            raise RuntimeError(f"No navigation response received for {url}")

        try:
            self.playwright_page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:  # pylint: disable=broad-except
            pass

        if (
            response.status >= 400
            and not bool(getattr(config, "PLAYWRIGHT_HEADLESS", True))
            and bool(getattr(config, "PLAYWRIGHT_WAIT_FOR_MANUAL_OK_ON_BLOCK", False))
        ):
            logging.warning(
                "Playwright received HTTP %s for %s. Complete any manual challenge in the browser window, then press Enter to continue.",
                response.status,
                url,
            )
            input()
            response = self.playwright_page.goto(self.playwright_page.url, wait_until="domcontentloaded")
            if response is None:
                raise RuntimeError(f"No navigation response received after manual intervention for {url}")
            try:
                self.playwright_page.wait_for_load_state("networkidle", timeout=5_000)
            except Exception:  # pylint: disable=broad-except
                pass

        html = self.playwright_page.content()
        request = requests.Request("GET", url, headers=dict(self.session.headers)).prepare()
        adapted = requests.Response()
        adapted.status_code = response.status
        adapted.url = self.playwright_page.url
        adapted._content = html.encode("utf-8")  # type: ignore[attr-defined]
        adapted.encoding = "utf-8"
        adapted.headers = requests.structures.CaseInsensitiveDict(response.headers)
        adapted.request = request
        adapted.reason = response.status_text
        return adapted

    def get_soup(self, url: str) -> Optional[BeautifulSoup]:
        """Fetch URL with retry + exponential backoff, then return BeautifulSoup."""
        for attempt in range(config.MAX_RETRIES + 1):
            try:
                response = self.fetch_url(url)
                response.raise_for_status()
                self.sleep_request_delay()
                return BeautifulSoup(response.text, "html.parser")
            except Exception as exc:  # pylint: disable=broad-except
                if attempt >= config.MAX_RETRIES:
                    logging.error("Request failed permanently: %s | %s", url, exc)
                    return None

                wait_time = config.RETRY_BACKOFF_SECONDS * (2**attempt)
                logging.warning(
                    "Request failed (attempt %s/%s): %s | sleeping %.1fs",
                    attempt + 1,
                    config.MAX_RETRIES,
                    url,
                    wait_time,
                )
                time.sleep(wait_time)

        return None

    # -----------------------------
    # Progress / failure files
    # -----------------------------
    def load_progress(self) -> Dict:
        """Load progress.json, or initialize default progress structure."""
        if config.PROGRESS_JSON.exists():
            try:
                return json.loads(config.PROGRESS_JSON.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logging.warning("Invalid progress.json found. Creating fresh progress state.")

        return {
            "completed_years": [],
            "current_year": None,
            "processed_case_urls": [],
            "failed_case_urls": [],
            "last_updated": now_iso(),
        }

    def save_progress(self) -> None:
        """Persist scraper progress to disk."""
        self.progress["processed_case_urls"] = sorted(self.processed_case_urls)
        self.progress["failed_case_urls"] = sorted(self.failed_case_urls)
        self.progress["last_updated"] = now_iso()

        config.PROGRESS_JSON.write_text(
            json.dumps(self.progress, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def ensure_failed_cases_csv_exists(self) -> None:
        """Create failed_cases.csv with header if it does not exist."""
        if not config.FAILED_CASES_CSV.exists():
            with config.FAILED_CASES_CSV.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["case_url", "year", "error", "last_attempt"])

    def append_failed_case(self, case_url: str, year: int, error: str) -> None:
        """Append one failed case record to failed_cases.csv."""
        with config.FAILED_CASES_CSV.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([case_url, year, error[:1000], now_iso()])

    # -----------------------------
    # Excel helpers
    # -----------------------------
    def load_existing_case_urls_from_excel(self) -> Set[str]:
        """Load case_url values from Excel for duplicate prevention across reruns."""
        if not config.OUTPUT_EXCEL.exists():
            return set()

        try:
            df = pd.read_excel(config.OUTPUT_EXCEL, engine="openpyxl")
            if "case_url" in df.columns:
                return {str(v).strip() for v in df["case_url"].dropna().tolist() if str(v).strip()}
            return set()
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("Could not load existing Excel URLs: %s", exc)
            return set()

    def append_rows_to_excel(self, rows: List[Dict]) -> int:
        """Append rows incrementally to Excel while preventing duplicates by case_url."""
        new_rows = []
        for row in rows:
            case_url = (row.get("case_url") or "").strip()
            if not case_url:
                continue
            if case_url in self.excel_case_urls:
                continue
            new_rows.append(row)
            self.excel_case_urls.add(case_url)

        if not new_rows:
            return 0

        if not config.OUTPUT_EXCEL.exists():
            df = pd.DataFrame(new_rows, columns=EXCEL_COLUMNS)
            df.to_excel(config.OUTPUT_EXCEL, index=False, engine="openpyxl")
            return len(new_rows)

        wb = load_workbook(config.OUTPUT_EXCEL)
        ws = wb.active

        for row in new_rows:
            ws.append([row.get(col, "") for col in EXCEL_COLUMNS])

        wb.save(config.OUTPUT_EXCEL)
        return len(new_rows)

    # -----------------------------
    # Scraper structure discovery
    # -----------------------------
    def extract_year_links(self) -> Dict[int, str]:
        """Extract all available Supreme Court year links from the browse page."""
        soup = self.get_soup(config.BASE_BROWSE_URL)
        if not soup:
            return {}

        year_links: Dict[int, str] = {}
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            match = re.search(r"/browse/supremecourt/(\d{4})/?", href)
            if not match:
                continue

            year = int(match.group(1))
            year_links[year] = urljoin(config.BASE_BROWSE_URL, href)

        return dict(sorted(year_links.items()))

    @staticmethod
    def build_year_url(year: int) -> str:
        """Build canonical Supreme Court browse URL for a given year."""
        base = config.BASE_BROWSE_URL.rstrip("/")
        return f"{base}/{year}/"

    def get_entire_year_url(self, year_url: str) -> Optional[str]:
        """From a year page, find the 'Entire Year' link."""
        soup = self.get_soup(year_url)
        if not soup:
            return None

        for a_tag in soup.find_all("a", href=True):
            if "entire year" in a_tag.get_text(" ", strip=True).lower():
                return urljoin(year_url, a_tag["href"])

        logging.warning("Entire Year link not found: %s", year_url)
        return None

    def get_case_links_from_results_page(self, url: str) -> Tuple[List[str], Optional[BeautifulSoup]]:
        """Extract case detail links from one paginated result page."""
        soup = self.get_soup(url)
        if not soup:
            return [], None

        case_links: Set[str] = set()
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if re.search(r"/doc/\d+/?", href):
                case_links.add(urljoin(url, href))

        return sorted(case_links), soup

    def get_next_page_url(self, soup: BeautifulSoup, current_url: str) -> Optional[str]:
        """Return the pagination 'Next' URL if available."""
        for a_tag in soup.find_all("a", href=True):
            label = a_tag.get_text(" ", strip=True).lower()
            if label == "next" or label.startswith("next "):
                return urljoin(current_url, a_tag["href"])
        return None

    # -----------------------------
    # Metadata extraction
    # -----------------------------
    @staticmethod
    def _clean_line_text(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    def _get_text_lines_above_rectangle(self, soup: BeautifulSoup) -> List[str]:
        """Try to keep only metadata area by removing likely judgment body containers."""
        cloned = BeautifulSoup(str(soup), "html.parser")

        # Remove common full-text containers and heavy blocks (judgment body lives here).
        body_selectors = [
            "pre",
            "div#judgments",
            "div.judgments",
            "div.judgement",
            "div#judgment",
            "div#original_text",
            "div.expanded_headline",
            "div.ad_doc",
            "div.doc_text",
        ]
        for selector in body_selectors:
            for node in cloned.select(selector):
                node.decompose()

        for noisy in cloned(["script", "style", "noscript"]):
            noisy.decompose()

        lines = []
        for line in cloned.get_text("\n").split("\n"):
            clean = self._clean_line_text(line)
            if clean:
                lines.append(clean)

        # Keep only early lines to reduce chance of pulling content body.
        return lines[:120]

    def extract_case_metadata(self, case_url: str, year: int) -> Dict:
        """Extract requested metadata fields from a case page."""
        soup = self.get_soup(case_url)
        if not soup:
            raise RuntimeError("Could not load case page")

        lines = self._get_text_lines_above_rectangle(soup)
        joined = "\n".join(lines)

        title = ""
        if soup.find("h2"):
            title = self._clean_line_text(soup.find("h2").get_text(" ", strip=True))
        elif soup.find("h1"):
            title = self._clean_line_text(soup.find("h1").get_text(" ", strip=True))
        elif soup.title:
            title = self._clean_line_text(soup.title.get_text(" ", strip=True))

        cites_match = re.search(r"\[\s*Cites[^\]]+\]", joined, flags=re.IGNORECASE)
        cites_block = cites_match.group(0) if cites_match else ""

        # Try best-known court text, fallback to any line mentioning court.
        court = ""
        for line in lines:
            if "supreme court of india" in line.lower():
                court = line
                break
        if not court:
            for line in lines:
                if "court" in line.lower():
                    court = line
                    break

        def extract_prefixed_value(prefix: str) -> str:
            pattern = re.compile(rf"^{re.escape(prefix)}\s*:?\s*(.*)$", re.IGNORECASE)
            for line in lines:
                match = pattern.match(line)
                if match:
                    return self._clean_line_text(match.group(1))
            return ""

        equivalent_citations = extract_prefixed_value("Equivalent citations")
        author = extract_prefixed_value("Author")
        bench = extract_prefixed_value("Bench")

        # Fallback if title was not in heading.
        if not title:
            for line in lines:
                if " vs " in line.lower() and " on " in line.lower():
                    title = line
                    break

        return {
            "year": year,
            "cites_block": cites_block,
            "court": court,
            "title": title,
            "equivalent_citations": equivalent_citations,
            "author": author,
            "bench": bench,
            "case_url": case_url,
            "scraped_at": now_iso(),
            "status": "success",
        }

    # -----------------------------
    # Crawl orchestration
    # -----------------------------
    def iter_year_case_links(self, year_url: str) -> Iterable[str]:
        """Yield all case links for a year by walking paginated 'Entire Year' results."""
        start_url = self.get_entire_year_url(year_url)
        if not start_url:
            return

        current = start_url
        seen_pages: Set[str] = set()

        while current and current not in seen_pages:
            seen_pages.add(current)
            case_links, soup = self.get_case_links_from_results_page(current)
            for case_link in case_links:
                yield case_link

            if not soup:
                return
            current = self.get_next_page_url(soup, current)

    def process_case_url(self, case_url: str, year: int) -> bool:
        """Process one case URL and persist success/failure results."""
        if case_url in self.processed_case_urls or case_url in self.excel_case_urls:
            return True

        try:
            row = self.extract_case_metadata(case_url, year)
            added = self.append_rows_to_excel([row])
            self.processed_case_urls.add(case_url)
            self.failed_case_urls.discard(case_url)
            self.save_progress()

            if added:
                logging.info("Saved case: %s", case_url)
            else:
                logging.info("Skipped duplicate (already in Excel): %s", case_url)
            return True

        except Exception as exc:  # pylint: disable=broad-except
            logging.error("Failed case: %s | %s", case_url, exc)
            self.failed_case_urls.add(case_url)
            self.append_failed_case(case_url, year, str(exc))
            self.save_progress()
            return False

    def scrape_year(self, year: int, year_url: str) -> None:
        """Scrape all case pages for one year."""
        logging.info("Starting year: %s", year)
        self.progress["current_year"] = year
        self.save_progress()

        count = 0
        for case_url in self.iter_year_case_links(year_url):
            self.process_case_url(case_url, year)
            count += 1
            if count % 25 == 0:
                logging.info("Year %s progress: %s cases visited", year, count)

        completed = set(self.progress.get("completed_years", []))
        completed.add(year)
        self.progress["completed_years"] = sorted(completed)
        self.progress["current_year"] = None
        self.save_progress()
        logging.info("Completed year: %s | cases visited: %s", year, count)

    def retry_failed_cases(self) -> None:
        """Retry failed case URLs listed in failed_cases.csv."""
        if not config.FAILED_CASES_CSV.exists():
            logging.info("No failed_cases.csv found. Nothing to retry.")
            return

        rows = []
        with config.FAILED_CASES_CSV.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        if not rows:
            logging.info("failed_cases.csv is empty. Nothing to retry.")
            return

        # Keep latest attempt per URL while preserving available year.
        latest: Dict[str, int] = {}
        for row in rows:
            url = (row.get("case_url") or "").strip()
            if not url:
                continue
            try:
                year = int(row.get("year", "0"))
            except ValueError:
                year = 0
            if url not in latest:
                latest[url] = year

        logging.info("Retrying failed cases: %s", len(latest))
        remaining_failed: List[Tuple[str, int, str]] = []

        for url, year in latest.items():
            ok = self.process_case_url(url, year)
            if not ok:
                remaining_failed.append((url, year, "still failing after retry"))

        # Rewrite failed_cases.csv with only remaining failed URLs.
        with config.FAILED_CASES_CSV.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["case_url", "year", "error", "last_attempt"])
            for url, year, error in remaining_failed:
                writer.writerow([url, year, error, now_iso()])

        logging.info("Retry complete. Remaining failed cases: %s", len(remaining_failed))


def parse_args() -> argparse.Namespace:
    """CLI argument parser."""
    parser = argparse.ArgumentParser(description="Indian Kanoon Supreme Court metadata scraper")

    parser.add_argument("--years", nargs="+", type=int, help="Explicit list of years. Example: --years 1950 1951")
    parser.add_argument("--start-year", type=int, help="Start year for range mode")
    parser.add_argument("--end-year", type=int, help="End year for range mode")

    parser.add_argument("--resume", action="store_true", help="Resume from progress.json")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry only failed case URLs from failed_cases.csv",
    )
    parser.add_argument(
        "--skip-year-discovery",
        action="store_true",
        help=(
            "Skip loading the browse root page and use direct year URLs only. "
            "Useful when the root page returns 403."
        ),
    )
    parser.add_argument(
        "--fetch-mode",
        choices=["requests", "playwright"],
        help="Choose page loading backend. Use 'playwright' when requests receives 403.",
    )

    return parser.parse_args()


def determine_target_years(args: argparse.Namespace, scraper: SupremeCourtScraper) -> List[int]:
    """Resolve target years from CLI args, config defaults, and resume state."""
    if args.years:
        years = sorted(set(args.years))
    elif args.start_year is not None and args.end_year is not None:
        if args.start_year > args.end_year:
            raise ValueError("start-year must be <= end-year")
        years = list(range(args.start_year, args.end_year + 1))
    elif config.YEAR_LIST:
        years = sorted(set(config.YEAR_LIST))
    else:
        years = list(range(config.START_YEAR, config.END_YEAR + 1))

    if args.resume:
        completed = set(scraper.progress.get("completed_years", []))
        years = [y for y in years if y not in completed]

    return years


def main() -> None:
    """Entry point."""
    setup_logging()
    args = parse_args()
    scraper = SupremeCourtScraper()
    if args.fetch_mode:
        scraper.fetch_mode = args.fetch_mode

    try:
        scraper.initialize_fetcher()
        scraper.warmup_session()

        logging.info("Scraper started")

        if args.retry_failed:
            scraper.retry_failed_cases()
            logging.info("Retry-failed mode complete")
            return

        target_years = determine_target_years(args, scraper)
        if not target_years:
            logging.info("No target years to process.")
            return

        year_links: Dict[int, str] = {}
        if args.skip_year_discovery:
            logging.info(
                "Skipping browse root discovery by CLI flag; using direct year URLs only."
            )
        else:
            year_links = scraper.extract_year_links()
            if year_links:
                logging.info("Discovered %s year links from browse page", len(year_links))
            else:
                logging.warning(
                    "Could not discover year links from %s. Falling back to direct year URLs.",
                    config.BASE_BROWSE_URL,
                )

        for year in target_years:
            year_url = year_links.get(year) or scraper.build_year_url(year)
            scraper.scrape_year(year, year_url)

        logging.info("Scraper finished")
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
