# Indian Kanoon Supreme Court Metadata Scraper

A production-oriented, restartable Python scraper that collects **Supreme Court case metadata** from Indian Kanoon:

- Start page: `https://indiankanoon.org/browse/supremecourt/`
- Supports year batching, resume, failed-case retry, and duplicate-safe incremental Excel writing.
- Extracts only metadata above the judgment content rectangle.

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
```

### 4) Run scraper (examples)

```bash
python scraper.py --years 1950 1951 1952
python scraper.py --start-year 1950 --end-year 1955
python scraper.py --resume
python scraper.py --retry-failed
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
python scraper.py --years 1950
```

Then inspect:

- `output/supreme_court_cases.xlsx`
- `scraper.log`
- `progress.json`
- `failed_cases.csv`
