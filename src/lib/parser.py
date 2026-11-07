import datetime
import io
import os
import re

import pandas as pd
import msoffcrypto
from openpyxl import load_workbook
import xlrd
from config import PASS_FILE
import warnings
warnings.filterwarnings("ignore")

def get_passwords(pw_file):
    if not os.path.exists(pw_file):
        return []
    with open(pw_file, 'r') as f:
        lines = [line.strip() for line in f.readlines() if line.strip() and not line.startswith('#')]
    return lines

def try_open_workbook(file_path, passwords):
    """
    Attempts to open an Excel file with a list of passwords.
    Returns a dataframe if successful, else None.
    """
    # 1. Try without password
    try:
        if file_path.endswith('.csv'):
            # Handle jagged CSVs (metadata rows having fewer cols than data)
            import csv
            max_cols = 0
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                reader = csv.reader(f)
                for row in reader:
                    if len(row) > max_cols:
                        max_cols = len(row)
                        
            # Read with generic names to prevent "Expected X fields saw Y"
            return pd.read_csv(file_path, header=None, names=range(max_cols), engine='python')
        return pd.read_excel(file_path)
    except Exception as e:
        if str(e) != "Can't find workbook in OLE2 compound document":
            print(f"Failed to read {file_path}: {e}")

    # 2. Try with passwords
    for pw in passwords:
        try:
            decrypted = io.BytesIO()
    
            with open(file_path, "rb") as f:
                office_file = msoffcrypto.OfficeFile(f)
                office_file.load_key(password=pw)
                office_file.decrypt(decrypted)
            
            decrypted.seek(0)
            df = pd.read_excel(decrypted)
            return df
        except:
            continue
            
    return None

def remove_leading_empty_columns(df):
    """
    Removes columns from the left if they are purely empty (NaN or empty strings).
    Stops at the first column that has data.
    """
    if df is None or df.empty:
        return df

    drop_indices = []
    num_cols = len(df.columns)
    
    for i in range(num_cols):
        col_data = df.iloc[:, i]
        
        # Check count of non-null
        if col_data.count() == 0:
            drop_indices.append(i)
            continue
            
        # Check if non-NA values are empty strings
        non_nans = col_data.dropna().astype(str).str.strip()
        if non_nans.eq('').all():
             drop_indices.append(i)
        else:
             # Found data column
             break
             
    if drop_indices:
        # Drop by index
        # We need to drop these column indices. 
        # Easier to keep the slice from first non-dropped index
        start_idx = len(drop_indices)
        df = df.iloc[:, start_idx:]
        
    return df

def find_table_start(df):
    """
    Identifies the header row based on column fill density.
    Returns (cleaned_df, header_row_index)
    """
    if df is None or df.empty:
        return None, 0
        
    # NOTE: Leading empty columns now handled by pipeline before calling this
    # df = remove_leading_empty_columns(df)

    max_density = 0
    header_idx = 0
    limit = min(30, len(df))
    
    for i in range(limit):
        row = df.iloc[i]
        non_null_count = row.count()
        if non_null_count > max_density:
            max_density = non_null_count
            header_idx = i
            
    # Set header
    # We take the row AT header_idx as columns
    new_header = df.iloc[header_idx].copy()
    
    # Custom grouping logic for merged/empty headers
    final_header = []
    i = 0
    while i < len(new_header):
        val = str(new_header.iloc[i]).strip()
        if val == '' or pd.isna(new_header.iloc[i]):
            # If start column is empty, just preserve it
            final_header.append(new_header.iloc[i])
            i += 1
            continue
            
        group = [val]
        j = i + 1
        while j < len(new_header):
            next_val = str(new_header.iloc[j]).strip()
            if next_val == '' or pd.isna(new_header.iloc[j]):
                group.append(next_val)
                j += 1
            else:
                break
        
        if len(group) == 1:
            final_header.append(group[0])
        else:
            final_header.extend([f"{group[0]}-{idx+1}" for idx in range(len(group))])
        i = j
    
    # Ensure uniqueness (pandas requirement for robust mapping/access)
    counts = {}
    unique_header = []
    for col in final_header:
        c_str = str(col)
        if c_str not in counts:
            counts[c_str] = 0
            unique_header.append(c_str)
        else:
            counts[c_str] += 1
            unique_header.append(f"{c_str}.{counts[c_str]}")
            
    new_header = pd.Series(unique_header, index=new_header.index)
    
    new_df = df[header_idx + 1:].copy()
    new_df.columns = new_header
    return new_df, header_idx

FOOTER_KEYWORDS = [
    "total", "closing balance", "grand total", "-- end of statement --", "** end of statement **",
    "statement summary",
    "unless constituent notifies the bank immediately of any discrepancy found by him in his statement of",
    "page no -",
]

# Word-bounded at alphanumeric edges: "total" must not match "TOTALENERGIES"
# or "UPI/subtotal@ybl" inside a transaction's narration.
_FOOTER_PATTERNS = [
    re.compile((r'\b' if k[0].isalnum() else '') + re.escape(k) + (r'\b' if k[-1].isalnum() else ''))
    for k in FOOTER_KEYWORDS
]

# A cell that is a date on its own: 05/01/2024, 5-Jan-24, 2024-01-05 [10:30:00].
_DATE_CELL = re.compile(r'^\s*(\d{1,2}[/\-. ][A-Za-z0-9]{1,9}[/\-. ]\d{2,4}|\d{4}-\d{2}-\d{2})([ T]\d{1,2}:\d{2}(:\d{2})?)?\s*$')


def _has_date_cell(cells):
    return any(isinstance(c, (pd.Timestamp, datetime.date)) or (isinstance(c, str) and _DATE_CELL.match(c))
               for c in cells)


def is_footer_row(cells):
    """
    Whether a row (its first few cells) marks the end of the transaction table.

    A row carrying a transaction date is a transaction, so for those only a
    cell that *starts* with a footer phrase counts ("Closing Balance ..." rows
    are often dated). Undated rows end the table if a footer phrase appears
    anywhere in them.
    """
    texts = [str(c).strip().lower() for c in cells if pd.notna(c) and str(c).strip()]
    if not texts:
        return False
    if _has_date_cell(cells):
        # Bare "total" is too common at the start of a narration ("TOTAL
        # MALL ...") to trust on a dated row unless it's the whole cell.
        return any(t.rstrip(': ') == "total" for t in texts) or any(
            p.match(t) for t in texts
            for k, p in zip(FOOTER_KEYWORDS, _FOOTER_PATTERNS) if k != "total")
    line = " ".join(texts)
    return any(p.search(line) for p in _FOOTER_PATTERNS)


def find_table_end(df):
    """
    Cut the table at its footer: a summary/total row (see is_footer_row) or
    three consecutive blank rows. Isolated blank rows inside or at the end of
    a statement are not an end -- remove_junk_rows drops them later.
    """
    if df is None or df.empty:
        return df

    check_cols = df.columns[:3]
    for i in range(len(df)):
        row = df.iloc[i]
        if row.count() == 0:
            if i + 2 < len(df) and df.iloc[i + 1].count() == 0 and df.iloc[i + 2].count() == 0:
                return df.iloc[:i]
            continue
        if is_footer_row(list(row[check_cols])):
            return df.iloc[:i]
    return df

def remove_junk_rows(df):
    """
    Removes rows that contain only asterisks or are empty after stripping asterisks and whitespace.
    Also removes rows where only 1 column is filled.
    """
    if df is None or df.empty:
        return df
        
    def is_junk(row):
        # 1. Check for separator junk (stars, dashes, dots)
        val = "".join(row.fillna('').astype(str))
        if val.strip().replace('*', '').replace('-', '').replace('.', '') == '':
            return True
            
        # 2. Check for "thin" rows (only 1 or 0 columns filled)
        # We count values that are not NaN and not just whitespace
        filled_count = 0
        for item in row:
            if pd.notna(item) and str(item).strip() != '':
                filled_count += 1
        
        if filled_count <= 1:
            return True
            
        return False
        
    return df[~df.apply(is_junk, axis=1)]

def remove_empty_junk_columns(df):
    """
    Removes columns that are effectively empty (all NaN or empty strings).
    """
    if df is None or df.empty:
        return df
        
    keep_indices = []
    num_cols = len(df.columns)
    
    for i in range(num_cols):
        col_data = df.iloc[:, i]
        
        if col_data.count() == 0:
            continue
            
        non_nans = col_data.dropna().astype(str).str.strip()
        if non_nans.eq('').all():
            continue
            
        keep_indices.append(i)
            
    if len(keep_indices) != num_cols:
        df = df.iloc[:, keep_indices]
        
    return df

