import pandas as pd
import os
import io
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
    
    # Forward fill merged/empty headers
    # Replace empty strings with NaN then ffill
    new_header = new_header.replace('', pd.NA).ffill()
    
    # Check if we have duplicate columns in header, pandas doesn't like that.
    # We will deduplicate them later or let pandas handle it? 
    # Better to keep as is for mapping.
    
    new_df = df[header_idx + 1:].copy()
    new_df.columns = new_header
    return new_df, header_idx

def find_table_end(df):
    """
    Heuristic to find the end of the table.
    Scans from top to bottom. If it hits a row with significant structure change 
    (e.g., "Total", "Closing Balance" in first col, or many nulls), it cuts off.
    """
    if df is None or df.empty:
        return df

    # We iterate and check for footer keywords or empty rows
    # Stop if 3 consecutive rows are mostly empty? 
    # Or stop at specific keywords?
    
    footer_keywords = ["total", "closing balance", "grand total", "-- end of statement --", "** end of statement **", 'statement summary', "Unless constituent notifies the bank immediately of any discrepancy found by him in his statement of", "Page No -"]
    
    end_idx = len(df)
    
    # We'll just list columns to check (usually first few)
    check_cols = df.columns[:3]
    
    for i in range(len(df)):
        row = df.iloc[i]
        
        # density check
        if row.count() == 0:
            # If completely empty, maybe end? Banks sometimes have blank lines.
            # Let's check next 2 lines. If all blank, then end.
            if i + 2 < len(df):
                next_1 = df.iloc[i+1].count()
                next_2 = df.iloc[i+2].count()
                if next_1 == 0 and next_2 == 0:
                    end_idx = i
                    break
            else:
                # End of file
                end_idx = i
                break
                
        # Keyword check
        # We join the first few cols to string to check for "Total" etc
        # Using .astype(str) handles non-string data safely
        line_str = " ".join(row[check_cols].fillna('').astype(str)).lower()
        if any(k in line_str for k in footer_keywords):
            end_idx = i
            break
            
    return df.iloc[:end_idx]

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

