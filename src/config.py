import os

# Base Directory
BASE_DIR = r"q:/banks"
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_DIR = os.path.join(DATA_DIR, "config")
STATEMENTS_DIR = os.path.join(DATA_DIR, "statements")

# Directories
RAW_DIR = os.path.join(STATEMENTS_DIR, "raw_statements")
ENRICHED_DIR = os.path.join(STATEMENTS_DIR, "enriched_statements")
FINAL_DIR = os.path.join(STATEMENTS_DIR, "final_statements")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
TXN_METADATA_DIR = os.path.join(DATA_DIR, "txn_metadata")
EXTRACTION_DIR = os.path.join(STATEMENTS_DIR, "raw_extracted_tables")
SRC_DIR = os.path.join(BASE_DIR, "src")

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

# Categorization
# Removed as it's now handled by rules. json/api based.

# Ensure directories exist
for d in [CONFIG_DIR, RAW_DIR, ENRICHED_DIR, FINAL_DIR, UPLOAD_DIR, TXN_METADATA_DIR]:
    if not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
