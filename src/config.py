import os

# Base Directory - Assuming script is run from project root or handling relative paths
# Hardcoding q:/banks as per project setup, but using os.path.join for subdirs
BASE_DIR = r"q:/banks"

# Directories
RAW_DIR = os.path.join(BASE_DIR, "raw_statements")
ENRICHED_DIR = os.path.join(BASE_DIR, "enriched_statements")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
FINAL_DIR = os.path.join(BASE_DIR, "final_statements")
SRC_DIR = os.path.join(BASE_DIR, "src")

# Files
PASS_FILE = os.path.join(SRC_DIR, "passwords.txt")
MAPPING_FILE = os.path.join(SRC_DIR, "column_mapping.csv")
MEGA_STATEMENT_PARQUET = os.path.join(ENRICHED_DIR, "mega_statement.parquet")
MEGA_STATEMENT_CSV = os.path.join(ENRICHED_DIR, "mega_statement.csv")
DUPLICATES_REPORT_CSV = os.path.join(ENRICHED_DIR, "duplicates_report.csv")

# Data Standard
STANDARD_COLUMNS = ["Date", "Description", "Debit", "Credit", "Balance", "RefNumber", "Amount", "TxnType"]

# Categorization
# Removed as it's now handled by rules. json/api based.

# Ensure directories exist
for d in [RAW_DIR, ENRICHED_DIR, FINAL_DIR, UPLOAD_DIR]:
    if not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
