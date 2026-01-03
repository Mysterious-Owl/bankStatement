import pandas as pd
import os
import hashlib
from lib.parser import get_passwords, try_open_workbook, find_table_start, find_table_end, remove_junk_rows
from lib.enricher import enricher
import config

def get_file_mapping(mapping_df, filename):
    file_map = mapping_df[mapping_df['FileName'] == filename]
    if file_map.empty:
        return {}
    
    valid_map = file_map.dropna(subset=['MappedTo'])
    valid_map = valid_map[valid_map['MappedTo'] != ""]
    return dict(zip(valid_map['RawColumn'], valid_map['MappedTo']))

def generate_txn_id(row):
    s = f"{row.get('Date')}{row.get('Description')}{row.get('Debit')}{row.get('Credit')}{row.get('Balance')}"
    return hashlib.md5(s.encode('utf-8')).hexdigest()

def main():
    if not os.path.exists(config.MAPPING_FILE):
        print("Mapping file not found! Please run src/generate_mapping.py first.")
        return

    mapping_df = pd.read_csv(config.MAPPING_FILE)
    passwords = get_passwords(config.PASS_FILE)
    
    all_txns = []
    
    import glob
    files = glob.glob(os.path.join(config.RAW_DIR, "*"))
    
    for f in files:
        if not (f.endswith('.xls') or f.endswith('.xlsx')):
            continue
            
        basename = os.path.basename(f)
        col_map = get_file_mapping(mapping_df, basename)
        if not col_map:
            print(f"No mapping found for {basename}, skipping...")
            continue
            
        print(f"Processing {basename}...")
        df = try_open_workbook(f, passwords)
        if df is None:
            print(f"Failed to open {basename}")
            continue
            
        cleaned_df, _ = find_table_start(df)
        cleaned_df = find_table_end(cleaned_df)
        cleaned_df = remove_junk_rows(cleaned_df)
        
        # Save raw extracted table
        if not os.path.exists(os.path.join(config.BASE_DIR, "raw_extracted_tables")):
             os.makedirs(os.path.join(config.BASE_DIR, "raw_extracted_tables"), exist_ok=True)
             
        raw_extracted_path = os.path.join(config.BASE_DIR, "raw_extracted_tables", f"{basename}.csv")
        cleaned_df.to_csv(raw_extracted_path, index=False)
        
        # Normalize raw columns to string
        cleaned_df.columns = [str(c) for c in cleaned_df.columns]
        
        # Invert mapping: Standard -> [RawCols]
        # col_map is {Raw: Std}
        # We need to be careful: col_map keys match cleaned_df.columns
        
        # Filter col_map to only include columns present in cleaned_df
        present_col_map = {k: v for k, v in col_map.items() if k in cleaned_df.columns}
        
        # Group by Standard Column
        std_to_raw = {}
        for raw, std in present_col_map.items():
            std_to_raw.setdefault(std, []).append(raw)
            
        # Initialize Final DF
        final_df = pd.DataFrame(index=cleaned_df.index)
        final_df['SourceFile'] = basename
        final_df['Remarks'] = "" # Initialize Remarks
        
        # 1. Handle Date
        # Strategy: Take first as primary. Compare others.
        date_cols = std_to_raw.get('Date', [])
        if date_cols:
            primary_date_col = date_cols[0]
            # FIX: dayfirst=True for Indian context
            final_df['Date'] = pd.to_datetime(cleaned_df[primary_date_col], errors='coerce', dayfirst=True)
            
            # Compare with others
            for sec_col in date_cols[1:]:
                # FIX: dayfirst=True
                sec_date_series = pd.to_datetime(cleaned_df[sec_col], errors='coerce', dayfirst=True)
                # Find where they differ (and both are not NaT)
                mask = (final_df['Date'] != sec_date_series) & (final_df['Date'].notna()) & (sec_date_series.notna())
                
                if mask.any():
                    # Format: "ValDate: 2023-01-01; "
                    diff_strs = sec_col + ": " + sec_date_series[mask].dt.strftime('%Y-%m-%d')
                    final_df.loc[mask, 'Remarks'] = final_df.loc[mask, 'Remarks'] + diff_strs + "; "
        else:
            final_df['Date'] = pd.NaT

        # 2. Handle Description
        # Strategy: Concatenate all mapped description columns
        desc_cols = std_to_raw.get('Description', [])
        if desc_cols:
            # We select all these Description columns. 
            # fillna('') to avoid "nan" in string join
            final_df['Description'] = cleaned_df[desc_cols].fillna('').astype(str).agg(' '.join, axis=1)
        else:
            final_df['Description'] = ""
            
        # 3. Handle Money and Other Columns (Debit, Credit, Balance, RefNumber)
        # Strategy: Take first mapped. If multiple map to Debit/Credit, user probably mapped separate columns. 
        # If user mapped "Withdrawal" -> Debit and "Dr" -> Debit, we might want to coalesce or sum?
        # For simplicity: Coalesce (take first non-null) or just take first?
        # Let's take first for now as per "Merge descriptions" instruction implies logic mainly for desc. 
        # But for numeric, usually it's one column.
        
        for col in ["Debit", "Credit", "Balance", "RefNumber"]:
            raw_cols = std_to_raw.get(col, [])
            if raw_cols:
                # Take the first one
                # TODO: If multiple columns, maybe sum them? (e.g. distinct specific user fees and taxes both mapped to Debit)
                # For safety, let's take the first one. 
                final_df[col] = cleaned_df[raw_cols[0]]
                if len(raw_cols) > 1:
                     print(f"Warning: Multiple columns mapped to {col} in {basename}: {raw_cols}. Using {raw_cols[0]}.")
            else:
                final_df[col] = 0 if col in ["Debit", "Credit", "Balance"] else ""

        # Clean Data
        for col in ["Debit", "Credit", "Balance"]:
            final_df[col] = pd.to_numeric(final_df[col], errors='coerce').fillna(0)
            
        final_df = final_df[~((final_df['Debit'] == 0) & (final_df['Credit'] == 0) & (final_df['Balance'] == 0))]
        
        # Bank Name
        if "HDFC" in basename.upper(): final_df['Bank'] = "HDFC"
        elif "SBI" in basename.upper(): final_df['Bank'] = "SBI"
        elif "IDFC" in basename.upper(): final_df['Bank'] = "IDFC"
        elif "PNB" in basename.upper(): final_df['Bank'] = "PNB"
        else: final_df['Bank'] = "Unknown"
        
        # Enrich
        final_df['Category'] = final_df['Description'].apply(enricher.categorize)
        final_df['Entity'] = final_df['Description'].apply(enricher.extract_entity)
        final_df['Tags'] = "" 
        
        all_txns.append(final_df)
        
    if all_txns:
        mega_df = pd.concat(all_txns, ignore_index=True)
        mega_df['TxnID'] = mega_df.apply(generate_txn_id, axis=1)
        
        before_count = len(mega_df)
        mega_df.drop_duplicates(subset=['TxnID'], inplace=True)
        print(f"Merged {len(all_txns)} files. Rows: {before_count} -> {len(mega_df)} (after dedup)")
        
        if not os.path.exists(config.ENRICHED_DIR): os.makedirs(config.ENRICHED_DIR)

        mega_df.to_parquet(config.MEGA_STATEMENT_PARQUET)
        mega_df.to_csv(config.MEGA_STATEMENT_CSV, index=False)
        print(f"Saved to {config.MEGA_STATEMENT_PARQUET}")
        
    else:
        print("No transactions processed.")

if __name__ == "__main__":
    main()
