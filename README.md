# Indian Kanoon Supreme Court Metadata Scraper

A production-oriented, restartable Python scraper that collects **Supreme Court case metadata** from Indian Kanoon:

- Start page: `https://indiankanoon.org/browse/supremecourt/`
- Supports year batching, resume, failed-case retry, and duplicate-safe incremental Excel writing.
- Extracts only metadata above the judgment content rectangle.
- Supports optional warm-up requests, optional extra headers, randomized per-request delay jitter, and Playwright-based browser fetching.

## What this scraper extracts per case

- `cites_block` (example: `[Cites 40, Cited by 657]`, if available)
- `court`
- `title`
- `equivalent_citations`
- `author`
- `bench`
- `case_url`
- `year`
- `scraped_at`
- `status`

## Project files

Generated and maintained by this project:

- `scraper.py` - main scraper implementation
- `config.py` - editable default configuration
- `requirements.txt` - exact dependencies
- `README.md` - usage and setup docs

Runtime files created during execution:

- `progress.json`
- `failed_cases.csv`
- `scraper.log`
- `output/supreme_court_cases.xlsx`

## Prerequisites

- Python 3.10+ recommended
- VS Code terminal

## VS Code terminal setup commands

### 1) Create a virtual environment

```bash
python -m venv .venv
```

### 2) Activate the virtual environment

**Windows (PowerShell):**

```powershell
.\.venv\Scripts\Activate.ps1
```

**macOS/Linux (bash/zsh):**

```bash
source .venv/bin/activate
```

### 3) Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

### 4) Run scraper (examples)

```bash
python scraper.py --years 1950 1951 1952
python scraper.py --start-year 1950 --end-year 1955
python scraper.py --resume
python scraper.py --retry-failed
python scraper.py --years 1950 --skip-year-discovery
python scraper.py --years 1950 --skip-year-discovery --fetch-mode playwright
```

## Batch execution modes

### Mode 1: Explicit year list

```bash
python scraper.py --years 1950 1951 1952
```

### Mode 2: Year range

```bash
python scraper.py --start-year 1950 --end-year 1955
```

### Mode 3: Resume from progress file

```bash
python scraper.py --resume --start-year 1950 --end-year 1960
```

- Uses `progress.json` to skip completed years and already-processed case URLs.

### Mode 4: Retry failed URLs only

```bash
python scraper.py --retry-failed
```

- Reads URLs from `failed_cases.csv` and retries only those cases.

## How resume works

The scraper stores runtime state in `progress.json`:

- `completed_years`
- `current_year`
- `processed_case_urls`
- `failed_case_urls`
- `last_updated`

Progress is continuously saved after each case attempt, so interruption-safe restart is supported.

## How failed-case retry works

- Any case that fails after request retries is appended to `failed_cases.csv`.
- Running `--retry-failed` loads those URLs and retries them.
- The failed file is rewritten to keep only still-failing URLs.

## Duplicate prevention

Duplicate rows are avoided by `case_url` checks against:

1. URLs already processed in `progress.json`
2. URLs already present in `output/supreme_court_cases.xlsx`

This makes repeated runs safe.

## Notes on extraction boundary

The scraper removes common judgment-body containers (`pre`, judgment text blocks, etc.) before text parsing, so extraction focuses on metadata above the content rectangle.

## 403 handling on browse root page

Some environments may receive `403 Forbidden` for:

- `https://indiankanoon.org/browse/supremecourt/`

The scraper now falls back to direct year URLs (for example, `.../browse/supremecourt/1950/`) so a blocked root browse page does not stop normal year-based runs.

If you want to avoid requesting the root browse page completely, run with:

```bash
python scraper.py --years 1950 --skip-year-discovery
```

Keeping a browser tab open does not help this script because it uses its own `requests.Session` (separate cookies/session from your browser).

If the site allows access in your browser but blocks the script, you can now copy the browser request's `Cookie` header into `config.py` as `COOKIE_HEADER`. That lets the scraper reuse the browser-validated session inside its own `requests.Session`.

If direct requests continue returning `403`, switch to the browser-backed fetcher:

```bash
python scraper.py --years 1950 --skip-year-discovery --fetch-mode playwright
```

That mode navigates pages in Chromium via Playwright, then feeds the rendered HTML into the existing parser/output pipeline.

If the site shows a Cloudflare verification page, the verification flow itself may be blocked by a browser extension, VPN, DNS filter, firewall rule, or network policy. The current default `config.py` uses a clean visible Playwright Chromium session and disables warm-up requests to reduce those conflicts.

If you still want to try real Edge instead, use this setup in `config.py`:

```python
FETCH_MODE = "playwright"
PLAYWRIGHT_BROWSER_CHANNEL = "msedge"
PLAYWRIGHT_HEADLESS = False
PLAYWRIGHT_USE_PERSISTENT_CONTEXT = True
PLAYWRIGHT_WAIT_FOR_MANUAL_OK_ON_BLOCK = True
```

Then run:

```bash
python scraper.py --years 1950 --skip-year-discovery
```

With the current default `config.py`, this opens a visible Playwright Chromium session and allows a manual verification step before the scraper retries the blocked navigation.

If you choose to re-enable persistent browser profiles later, keep Chromium and Edge in separate profile folders. This project now defaults to:

- `.playwright-chromium-profile` for Chromium-based Playwright sessions
- `.playwright-edge-profile` for Edge sessions

That avoids the `edge://resources/*` profile corruption issue that can crash Chromium at startup.

If you see a message about `challenges.cloudflare.com` being blocked, check these before rerunning:

- Turn off ad blockers, privacy extensions, or script blockers in your normal browser.
- Disable VPN, proxy, Pi-hole, NextDNS, Little Snitch, or similar filtering temporarily.
- Try a different network, such as mobile hotspot, if your current network blocks Cloudflare challenge traffic.
- If your firewall or DNS tooling is managed by your workplace, the scraper cannot bypass that in code.

## Optional anti-blocking knobs

To reduce bot-like request patterns, this scraper includes optional controls in `config.py`:

- `ENABLE_EXTRA_HEADERS` + `EXTRA_HEADERS` for additional browser-like headers.
- `COOKIE_HEADER` to reuse cookies copied from a browser request.
- `RANDOMIZE_DELAY` + `REQUEST_DELAY_JITTER_SECONDS` to randomize inter-request wait time.
- `WARMUP_ENABLED` + `WARMUP_URLS` to make pre-crawl warm-up requests before year discovery and scraping.
- `FETCH_MODE`, `PLAYWRIGHT_HEADLESS`, and `PLAYWRIGHT_NAVIGATION_TIMEOUT_MS` to control browser-backed fetching.
- `PLAYWRIGHT_BROWSER_CHANNEL`, `PLAYWRIGHT_USE_PERSISTENT_CONTEXT`, `PLAYWRIGHT_PROFILE_DIR`, `PLAYWRIGHT_EDGE_PROFILE_DIR`, `PLAYWRIGHT_FALLBACK_TO_NON_PERSISTENT`, `PLAYWRIGHT_LOCALE`, `PLAYWRIGHT_TIMEZONE_ID`, and `PLAYWRIGHT_WAIT_FOR_MANUAL_OK_ON_BLOCK` for browser selection, optional persistent profiles, and manual unblock behavior.

These do not guarantee bypassing 403 responses, but they can improve resilience in some environments.

## Config customization

Edit `config.py` for defaults such as:

- Year defaults (`START_YEAR`, `END_YEAR`, `YEAR_LIST`)
- Output files
- Delay/timeout/retry settings
- User-Agent

CLI arguments always override year defaults from config.

## Recommended first run (small batch)

Start with one year to validate environment and output shape:

```bash
python scraper.py --years 1950 --skip-year-discovery
```

Then inspect:

- `output/supreme_court_cases.xlsx`
- `scraper.log`
- `progress.json`
- `failed_cases.csv`
