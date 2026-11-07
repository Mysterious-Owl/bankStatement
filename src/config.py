"""
Paths and settings.

Machine- and person-specific values come from a `.env` file at the repo root
(see `.env.example`), so they stay out of version control. Every setting has a
generic default, so the app also runs with no `.env` at all.
"""
import os

from dotenv import load_dotenv

# Repo root: the folder above src/. Derived, not hard-coded, so a checkout
# works wherever it lives.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(BASE_DIR, "src")

# Real environment variables win over .env, so a one-off override works.
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _env_list(name, default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# Where statements, mappings, rules and annotations live.
DATA_DIR = os.path.abspath(os.getenv("BANKS_DATA_DIR") or os.path.join(BASE_DIR, "data"))

# Bank names offered when uploading and recognised in statement filenames.
# Banks added through bank_names.csv are merged in at runtime.
KNOWN_BANKS = _env_list("KNOWN_BANKS", ["HDFC", "SBI", "ICICI", "AXIS", "KOTAK", "IDFC", "PNB"])

CONFIG_DIR = os.path.join(DATA_DIR, "config")
STATEMENTS_DIR = os.path.join(DATA_DIR, "statements")

# Directories
RAW_DIR = os.path.join(STATEMENTS_DIR, "raw_statements")
ENRICHED_DIR = os.path.join(STATEMENTS_DIR, "enriched_statements")
FINAL_DIR = os.path.join(STATEMENTS_DIR, "final_statements")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
TXN_METADATA_DIR = os.path.join(DATA_DIR, "txn_metadata")
EXTRACTION_DIR = os.path.join(STATEMENTS_DIR, "raw_extracted_tables")

# Config Files
PASS_FILE = os.path.join(CONFIG_DIR, "passwords.txt")
MAPPING_FILE = os.path.join(CONFIG_DIR, "column_mapping.csv")
RULES_FILE = os.path.join(CONFIG_DIR, "rules.json")
CATEGORIES_FILE = os.path.join(CONFIG_DIR, "categories.json")
TAGS_FILE = os.path.join(CONFIG_DIR, "tags.json")
REMARKS_FILE = os.path.join(CONFIG_DIR, "remarks.json")
# Unified annotation store (tags + remarks + description overrides + self
# transfers). Supersedes TAGS_FILE / REMARKS_FILE / SELF_TRANSFERS_FILE, which
# are migrated on first load and then left on disk as a fallback.
ANNOTATIONS_FILE = os.path.join(CONFIG_DIR, "annotations.json")
BANKS_FILE = os.path.join(CONFIG_DIR, "bank_names.csv")
# Spending budgets per category/tag (see analytics.budget_status)
BUDGETS_FILE = os.path.join(CONFIG_DIR, "budgets.json")
SELF_TRANSFERS_FILE = os.path.join(TXN_METADATA_DIR, "self_transfers.json")

# Data Files
MEGA_STATEMENT_CSV = os.path.join(ENRICHED_DIR, "mega_statement.csv")
DUPLICATES_REPORT_CSV = os.path.join(ENRICHED_DIR, "duplicates_report.csv")

# Data Standard
STANDARD_COLUMNS = ["Date", "Description", "Debit", "Credit", "Balance", "RefNumber", "Amount", "TxnType"]

# Ensure directories exist
for d in [CONFIG_DIR, RAW_DIR, ENRICHED_DIR, FINAL_DIR, UPLOAD_DIR, TXN_METADATA_DIR]:
    os.makedirs(d, exist_ok=True)
