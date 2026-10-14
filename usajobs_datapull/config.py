"""All settings and constants. Nothing else calls os.getenv().

Paths are fixed and relative to the repo.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- paths: fixed, relative to the repo, never a home directory ------------
DATA = ROOT / "data"
REFERENCE = DATA / "reference"
CODELISTS = REFERENCE / "codelists"   # USAJOBS code lists, one folder per fetch date
API_PROBES = REFERENCE / "api-probes" # `probe` reports: what the API allowed, one file per run
RAW = DATA / "raw"                    # untouched API pages, one folder per snapshot
WORK_DB = DATA / "work.db"            # SQLite: postings, quarantine, chunks, embeddings, runs
LOGS = DATA / "logs"
REPORTS = DATA / "reports"            # data-quality reports, one per stage run
MODELS = DATA / "models"              # downloaded embedding models (Hugging Face cache)
DIST = ROOT / "dist"
BUNDLE_DIR = DIST / "bundle"          # the built bundle, ready to publish

# --- credentials (secrets: .env only) --------------------------------------
USAJOBS_API_KEY = os.getenv("USAJOBS_API_KEY")
USAJOBS_USER_AGENT = os.getenv("USAJOBS_USER_AGENT")
HF_TOKEN = os.getenv("HF_TOKEN")

# --- publishing ------------------------------------------------------------
HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "cccc864/usajobs-postings")
# publish refuses to upload to a public dataset unless this is "false"
HF_DATASET_PRIVATE = os.getenv("HF_DATASET_PRIVATE", "true").strip().lower() not in ("false", "0", "no")

# --- scope -----------------------------------------------------------------
JOB_SERIES = os.getenv("JOB_SERIES", "2210")   # semicolon-separated, e.g. "2210;0854"
SNAPSHOT = os.getenv("SNAPSHOT", "live")       # "live" or e.g. "archive-2024-11-12"

# --- models ----------------------------------------------------------------
# ONNX weights, same as Transformers.js loads in the browser. Options: embedders.PROFILES.
EMBED_MODEL = os.getenv("EMBED_MODEL", "Xenova/bge-small-en-v1.5")

# --- USAJOBS API -----------------------------------------------------------
USAJOBS_SEARCH_URL = "https://data.usajobs.gov/api/Search"
USAJOBS_CODELIST_URL = "https://data.usajobs.gov/api/codelist/"
RESULTS_PER_PAGE = 500  # documented maximum (1000 also works)
SEARCH_RESULT_WINDOW = 10_000  # observed: one query never returns more than this many results

# --- probe ----------------------------------------------------------------
PROBE_PAGE_SIZES = (25, 100, 500, 1000)
PROBE_MAX_BURST = 100         # upper bound for `probe --burst N`
PROBE_RATE_STEPS = (1, 2, 4, 8)   # `probe --rate`: target requests/second, one step at a time
PROBE_RATE_STEP_REQUESTS = 30     # requests per step; worst case 120 requests in about a minute

# --- being a polite API client (constants, not settings) -------------------
POLITE_DELAY_S = 1.0          # minimum gap between requests
RATE_LIMIT_FLOOR = 10         # if X-RateLimit-Remaining drops below this, wait for reset
DEFAULT_WAIT_S = 60           # wait when a 429 gives no Retry-After / reset header
MAX_WAIT_S = 900              # never sleep longer than 15 min on the server's say-so; stop instead
MAX_RETRIES = 5               # per request
BACKOFF_CAP_S = 60            # exponential backoff 1, 2, 4, 8, 16 ... capped here
REQUEST_TIMEOUT_S = (10, 60)  # (connect, read)
MAX_RUNTIME_MIN = 30          # whole-run ceiling
MAX_PAGES = 40                # hard stop on pagination, whatever the API claims

# --- data quality (normalize) ----------------------------------------------
HOURS_PER_YEAR = 2087         # OPM's standard for converting hourly federal pay to annual
SALARY_SANE_MIN = 10_000      # annualized salaries outside this range are flagged
SALARY_SANE_MAX = 1_000_000
BROAD_NOTICE_SERIES = 10      # postings listing this many series (or locations) are "broad notices"
BROAD_NOTICE_LOCATIONS = 50

# --- chunking --------------------------------------------------------------
MAX_CHUNK_WORDS = 180         # ~240 tokens; well under the model's 512-token limit
BOILERPLATE_MIN_POSTINGS = 5  # a sentence repeated in at least this many postings...
BOILERPLATE_SHARE = 0.10      # ...and in at least this share of them is boilerplate
BOILERPLATE_MIN_CHARS = 40    # short sentences ("Duties") are never treated as boilerplate

# --- embedding / bundle ----------------------------------------------------
EMBED_BATCH = 32
BM25_K1 = 1.2
BM25_B = 0.75
CONFORMANCE_K = 5
CONFORMANCE_QUERIES = (
    ("cybersecurity analyst incident response", "dense"),
    ("cloud infrastructure engineer", "dense"),
    ("help desk customer support technician", "dense"),
    ("database administrator data management", "dense"),
    ("GS-13 TS/SCI", "lexical"),
    ("remote software developer python", "lexical"),
    ("project manager", "lexical"),
)

SECRETS = ("USAJOBS_API_KEY", "HF_TOKEN")
SETTINGS = (
    "USAJOBS_API_KEY", "USAJOBS_USER_AGENT", "HF_TOKEN",
    "HF_DATASET_REPO", "HF_DATASET_PRIVATE", "JOB_SERIES", "SNAPSHOT", "EMBED_MODEL",
)


def require(*names: str) -> None:
    """Fail loudly and early if a needed setting is missing.

    Each stage calls this at startup with only what it actually needs, so a
    missing key produces a clear message naming the variable and the file to
    put it in -- not a stack trace 200 postings into a run.
    """
    missing = [n for n in names if not globals().get(n)]
    if missing:
        print(f"Missing required setting(s): {', '.join(missing)}", file=sys.stderr)
        print(f"Set them in {ROOT / '.env'} -- see .env.example", file=sys.stderr)
        raise SystemExit(1)


def ensure_dirs() -> None:
    for d in (CODELISTS, API_PROBES, RAW, LOGS, REPORTS, MODELS, DIST):
        d.mkdir(parents=True, exist_ok=True)
