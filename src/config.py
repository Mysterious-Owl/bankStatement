import os

# Base Directory - Assuming script is run from project root or handling relative paths
# Hardcoding q:/banks as per project setup, but using os.path.join for subdirs
BASE_DIR = r"q:/banks"

# Directories
RAW_DIR = os.path.join(BASE_DIR, "raw_statements")
ENRICHED_DIR = os.path.join(BASE_DIR, "enriched_statements")
FINAL_DIR = os.path.join(BASE_DIR, "final_statements")
SRC_DIR = os.path.join(BASE_DIR, "src")

# Files
PASS_FILE = os.path.join(SRC_DIR, "passwords.txt")
MAPPING_FILE = os.path.join(RAW_DIR, "column_mapping.csv")
MEGA_STATEMENT_PARQUET = os.path.join(ENRICHED_DIR, "mega_statement.parquet")
MEGA_STATEMENT_CSV = os.path.join(ENRICHED_DIR, "mega_statement.csv")

# Data Standard
STANDARD_COLUMNS = ["Date", "Description", "Debit", "Credit", "Balance", "RefNumber"]

# Categorization
CATEGORIES = {
    "Food": ["swiggy", "zomato", "restaurant", "burger", "pizza", "coffee", "starbucks", "mcdonalds", "kfc", "dominos", "food"],
    "Transport": ["uber", "ola", "metro", "fuel", "hpcl", "bpcl", "petrol", "parking", "fastag", "shell", "indian oil"],
    "Shopping": ["amazon", "flipkart", "myntra", "zudio", "retail", "mart", "ikea", "decathlon", "ajio"],
    "Utilities": ["bescom", "water", "gas", "bill", "recharge", "jio", "airtel", "vi", "bsnl", "electricity"],
    "Finance": ["emi", "loan", "interest", "bank", "atm", "withdraw", "fund", "insurance", "premium"],
    "Investment": ["zerodha", "groww", "kuvera", "ppf", "nps", "sip", "mutual fund", "stocks"],
    "Health": ["pharmacy", "doctor", "hospital", "clinic", "medplus", "apollo", "1mg", "practo"],
    "Entertainment": ["netflix", "spotify", "hotstar", "cinema", "bookmyshow", "pvr", "inox", "youtube"]
}
