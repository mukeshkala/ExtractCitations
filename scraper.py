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
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
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
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            }
        )

        self.progress = self.load_progress()
        self.processed_case_urls: Set[str] = set(self.progress.get("processed_case_urls", []))
        self.failed_case_urls: Set[str] = set(self.progress.get("failed_case_urls", []))
        self.excel_case_urls: Set[str] = self.load_existing_case_urls_from_excel()

        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self.ensure_failed_cases_csv_exists()

    # -----------------------------
    # HTTP helpers
    # -----------------------------
    def get_soup(self, url: str) -> Optional[BeautifulSoup]:
        """Fetch URL with retry + exponential backoff, then return BeautifulSoup."""
        for attempt in range(config.MAX_RETRIES + 1):
            try:
                response = self.session.get(url, timeout=config.REQUEST_TIMEOUT)
                response.raise_for_status()
                time.sleep(config.REQUEST_DELAY_SECONDS)
                return BeautifulSoup(response.text, "html.parser")
            except requests.RequestException as exc:
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

    logging.info("Scraper started")

    if args.retry_failed:
        scraper.retry_failed_cases()
        logging.info("Retry-failed mode complete")
        return

    target_years = determine_target_years(args, scraper)
    if not target_years:
        logging.info("No target years to process.")
        return

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


if __name__ == "__main__":
    main()
