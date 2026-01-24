import pandas as pd
import os
import hashlib
from lib.parser import get_passwords, try_open_workbook, find_table_start, find_table_end, remove_junk_rows, remove_leading_empty_columns, remove_empty_junk_columns
import config

def get_file_mapping(mapping_df, filename):
    file_map = mapping_df[mapping_df['FileName'] == filename]
    if file_map.empty:
        return {}
    
    valid_map = file_map.dropna(subset=['MappedTo'])
    valid_map = valid_map[valid_map['MappedTo'] != ""]
    return dict(zip(valid_map['RawColumn'], valid_map['MappedTo']))

def generate_txn_id(row):
    s = f"{row.get('Date')}{row.get('Description')}{row.get('Debit')}{row.get('Credit')}{row.get('Balance')}{row.get('RefNumber')}"
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
        if not (f.endswith('.xls') or f.endswith('.xlsx') or f.endswith('.csv')):
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
            
        # 1. Remove Leading Empty Columns (Indentation)
        df = remove_leading_empty_columns(df)
        
        # 2. Find Table Header/Start
        cleaned_df, _ = find_table_start(df)
        
        # 3. Find Table End/Footer
        cleaned_df = find_table_end(cleaned_df)
        
        # 4. Remove Junk Rows (Separators)
        cleaned_df = remove_junk_rows(cleaned_df)
        
        # 5. Remove Empty Junk Columns (post-cleanup)
        cleaned_df = remove_empty_junk_columns(cleaned_df)
        
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
            # Join all mapped columns with space, replace newlines/tabs with space, collapse multi-spaces, and strip
            def clean_desc(row):
                text = " ".join(row.fillna('').astype(str))
                # Replace newlines and tabs with spaces
                text = text.replace('\n', '').replace('\r', '').replace('\t', ' ')
                # Collapse multiple spaces and strip
                return " ".join(text.split()).strip()
            
            final_df['Description'] = cleaned_df[desc_cols].apply(clean_desc, axis=1)
        else:
            final_df['Description'] = ""
            
        # 3. Handle Money and Other Columns (Debit, Credit, Balance, RefNumber)
        # Strategy: Take first mapped. If multiple map to Debit/Credit, user probably mapped separate columns. 
        # If user mapped "Withdrawal" -> Debit and "Dr" -> Debit, we might want to coalesce or sum?
        # For simplicity: Coalesce (take first non-null) or just take first?
        # Let's take first for now as per "Merge descriptions" instruction implies logic mainly for desc. 
        # But for numeric, usually it's one column.
        
        if "PPBL" in basename.upper(): 
            print('PPBL')

        # New: Handle "Amount" + "Type" schema
        amt_cols = std_to_raw.get('Amount', [])
        type_cols = std_to_raw.get('TxnType', [])
        
        has_split_amt = bool(amt_cols and type_cols)
        
        for col in ["Debit", "Credit", "Balance", "RefNumber"]:
            raw_cols = std_to_raw.get(col, [])
            if raw_cols:
                final_df[col] = cleaned_df[raw_cols[0]]
            else:
                final_df[col] = 0 if col in ["Debit", "Credit", "Balance"] else ""

        # If we have Amount + Type, populate Debit/Credit from it overrides
        if has_split_amt:
             raw_amt = cleaned_df[amt_cols[0]]
             raw_type = cleaned_df[type_cols[0]].astype(str).str.lower()
             
             # Initialize if not present
             if 'Debit' not in final_df.columns: final_df['Debit'] = 0
             if 'Credit' not in final_df.columns: final_df['Credit'] = 0
             
             # Convert amount to float first
             # We reuse clean_currency logic later but need basic float here
             # Let's defer strict cleaning, just ensure we can parse
             # Helper to parse amount loosely
             def parse_float(x):
                 import re
                 try:
                     return float(re.search(r'\d+\.?\d*', str(x).replace(',','')).group(0))
                 except: return 0.0
             
             floats = raw_amt.apply(parse_float)
             
             # Check for 'debit' or 'dr' in type
             is_credit = raw_type.str.contains('credit') | raw_type.str.contains('cr') | raw_type.str.contains('c')
             is_debit = raw_type.str.contains('debit') | raw_type.str.contains('dr') | raw_type.str.contains('d')
             
             # If mapping exists, overwrite
             # Note: This overwrites any "Debit" / "Credit" distinct columns if they existed via other mapping.
             # We assume if user mapped Amount/Type, they want this.
             
             final_df.loc[is_debit, 'Debit'] = floats[is_debit]
             final_df.loc[is_credit, 'Credit'] = floats[is_credit]

        # Clean Data (Handle commas, Cr/Dr suffixes)
        def clean_currency(val, is_balance=False):
            if pd.isna(val): return 0.0
            s = str(val).strip().lower()
            if not s: return 0.0
            
            # Determine sign magnitude
            sign = 1.0
            if 'dr' in s:
                if is_balance: sign = -1.0
            
            # Remove alphabets and commas
            clean_s = s.replace(',', '').replace('cr', '').replace('dr', '').replace(' ', '')
            
            import re
            match = re.search(r'-?\d+\.?\d*', clean_s)
            if match:
                try:
                    return float(match.group(0)) * sign
                except:
                    return 0.0
            return 0.0

        for col in ["Debit", "Credit", "Balance"]:
            is_bal = (col == "Balance")
            final_df[col] = final_df[col].apply(lambda x: clean_currency(x, is_balance=is_bal))
            
        final_df = final_df[~((final_df['Debit'] == 0) & (final_df['Credit'] == 0) & (final_df['Balance'] == 0))]
        
        # --- Handle Intra-Day Sorting (Index Normalization) ---
        # Bank statements can be Ascending (Oldest Top) or Descending (Newest Top).
        # We want SortIndex to always represent time order (Low = Early, High = Late).
        
        valid_dates = final_df['Date'].dropna()
        is_descending = False
        
        if len(valid_dates) > 1:
            first_date = valid_dates.iloc[0]
            last_date = valid_dates.iloc[-1]
            if first_date > last_date:
                is_descending = True
        
        if is_descending:
            # If Newest Top (Row 0 is late), we want SortIndex to be High for Row 0.
            # So we invert the index: 0 -> N, 1 -> N-1 ...
            final_df['SortIndex'] = range(len(final_df)-1, -1, -1)
        else:
            # If Oldest Top (Row 0 is early), SortIndex = 0, 1...
            final_df['SortIndex'] = range(len(final_df))
            
        final_df['OriginalIndex'] = final_df.index # Keep for reference
        
        # Bank Name
        if "HDFC" in basename.upper(): final_df['Bank'] = "HDFC"
        elif "SBI" in basename.upper(): final_df['Bank'] = "SBI"
        elif "IDFC" in basename.upper(): final_df['Bank'] = "IDFC"
        elif "PNB" in basename.upper(): final_df['Bank'] = "PNB"
        elif "KOTAK" in basename.upper(): final_df['Bank'] = "KOTAK"
        elif "PPBL" in basename.upper(): final_df['Bank'] = "PPBL"
        elif "UNITYMOM" in basename.upper(): final_df['Bank'] = "UNITYMOM"
        elif "UNITYTAN" in basename.upper(): final_df['Bank'] = "UNITYTAN"
        else: final_df['Bank'] = "Unknown"
        
        all_txns.append(final_df)
        
    if all_txns:
        mega_df = pd.concat(all_txns, ignore_index=True)
        
        # Sort by Date, then Normalized SortIndex (Time Order), then Bank
        mega_df.sort_values(by=['Date', 'SortIndex', 'Bank'], inplace=True)
        
        mega_df['TxnID'] = mega_df.apply(generate_txn_id, axis=1)
        
        # --- Duplicate Reporting Logic ---
        # Find all duplicates (keep=False marks all duplicates as True)
        dupes_mask = mega_df.duplicated(subset=['TxnID'], keep=False)
        
        if dupes_mask.any():
            dupes_df = mega_df[dupes_mask].copy()
            # We want to group by TxnID to keep pairs together
            # We also want to determine which one WOULD be kept (keep='first')
            
            report_rows = []
            # Group by TxnID
            grouped = dupes_df.groupby('TxnID')
            
            for txn_id, group in grouped:
                # The pipeline uses drop_duplicates(keep='first') by default later
                # So the first row in the group (as per original order) is 'Kept', others 'Deleted'
                # Note: group preserves original index/order if not sorted, but groupby might not.
                # Let's ensure we respect the original appearance order.
                # However, groupby preserves order within groups usually?
                # Let's safely sort by index if needed, or rely on iteration order.
                
                # To be sure of 'keep=first' behavior matching, we should look at original indices.
                # But here we just want to flag them.
                
                # Let's create a local copy to modify
                g = group.copy()
                g['DuplicateStatus'] = 'Deleted'
                g.iloc[0, g.columns.get_loc('DuplicateStatus')] = 'Kept' # Mark first as kept
                
                # Reorder columns to put Status first
                cols = ['DuplicateStatus'] + [c for c in g.columns if c != 'DuplicateStatus']
                g = g[cols]
                
                report_rows.append(g)
                # Add empty row for separation
                empty_row = pd.DataFrame([[''] * len(cols)], columns=cols)
                report_rows.append(empty_row)
            
            if report_rows:
                full_report = pd.concat(report_rows, ignore_index=True)
                full_report.to_csv(config.DUPLICATES_REPORT_CSV, index=False)
                print(f"Duplicates report saved to {config.DUPLICATES_REPORT_CSV}")
        else:
            print("No duplicates found.")

        # Dedup (Maintain original behavior)
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
