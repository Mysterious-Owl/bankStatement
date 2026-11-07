import os
import pandas as pd
from api import ingest_mapping
from lib.parser import get_passwords, try_open_workbook, find_table_start
import config

def generate_column_mapping(raw_headers, banks, output_csv):
    mapping_data = []
    
    for file_name, headers in raw_headers.items():
        bank = banks.get(file_name, "Other")
        suggested = ingest_mapping.suggest_mapping(headers)

        for col in headers:
            col_str = str(col).lower()
            best_match = suggested.get(col, "")

            # Catch-all: this script maps every remaining column to Description
            # rather than leaving it blank, on the assumption that anything a
            # statement carries alongside the amounts is narrative text.
            if not best_match and not any(x in col_str for x in ["dr / cr", "sl. no."]):
                best_match = "Description"

            mapping_data.append({
                "FileName": file_name,
                "Bank": bank,
                "RawColumn": col,
                "MappedTo": best_match
            })
            
    if mapping_data:
        new_map_df = pd.DataFrame(mapping_data)
        if os.path.exists(output_csv):
            existing_df = pd.read_csv(output_csv)
            existing_df = existing_df.fillna('')
            combined_df = pd.concat([existing_df, new_map_df], ignore_index=True)
            combined_df.to_csv(output_csv, index=False)
        else:
            new_map_df.to_csv(output_csv, index=False)
        print(f"Updated {output_csv} with {len(raw_headers)} new files")
    else:
        print("No new headers found to map.")

def main():
    passwords = get_passwords(config.PASS_FILE)
    import glob
    files = glob.glob(os.path.join(config.RAW_DIR, "*"))
    
    # Load existing mappings
    existing_files = set()
    if os.path.exists(config.MAPPING_FILE):
        m_df = pd.read_csv(config.MAPPING_FILE)
        if 'FileName' in m_df.columns:
            existing_files = set(m_df['FileName'].unique())

    all_headers = {}
    banks = {}
    
    # Load from registry
    known_banks = ["HDFC", "SBI", "IDFC", "PNB", "KOTAK", "PPBL", "UNITYMOM", "UNITYTAN", "AXIS", "ICICI"]
    if os.path.exists(config.BANKS_FILE):
        try:
            b_df = pd.read_csv(config.BANKS_FILE)
            known_banks = list(set(known_banks + b_df['Name'].astype(str).tolist()))
        except:
            pass

    for f in files:
        fname = os.path.basename(f)
        if not (f.endswith('.xls') or f.endswith('.xlsx') or f.endswith('.csv')):
            continue
            
        if fname in existing_files:
            continue
            
        print(f"Scanning new file: {fname}...")
        df = try_open_workbook(f, passwords)
        if df is None:
            print(f"Skipping {f} (Read Failed)")
            continue
            
        cleaned_df, _ = find_table_start(df)
        if cleaned_df is not None:
             all_headers[fname] = cleaned_df.columns.tolist()
             # Detect bank
             detected = "Other"
             fname_upper = fname.upper()
             for b in known_banks:
                 if b in fname_upper:
                     detected = b
                     break
             banks[fname] = detected
             
    generate_column_mapping(all_headers, banks, config.MAPPING_FILE)

if __name__ == "__main__":
    main()
