import os
import pandas as pd
from lib.parser import get_passwords, try_open_workbook, find_table_start
import config

def generate_column_mapping(raw_headers, output_csv):
    mapping_data = []
    
    for file_name, headers in raw_headers.items():
        for col in headers:
            best_match = ""
            col_str = str(col).lower()
            
            # Heuristics
            if "date" in col_str: best_match = "Date"
            elif any(x in col_str for x in ["value dt"]): best_match = "Date"
            elif any(x in col_str for x in ["desc", "particular", "narration", "details"]): best_match = "Description"
            elif any(x in col_str for x in ["debit", "dr", "withdrawal"]): best_match = "Debit"
            elif any(x in col_str for x in ["credit", "cr", "deposit"]): best_match = "Credit"
            elif "bal" in col_str: best_match = "Balance"
            elif any(x in col_str for x in ["ref", "chq", "txn", "cheque"]): best_match = "RefNumber"
            
            mapping_data.append({
                "FileName": file_name,
                "RawColumn": col,
                "MappedTo": best_match
            })
            
    if mapping_data:
        map_df = pd.DataFrame(mapping_data)
        map_df.to_csv(output_csv, index=False)
        print(f"Generated {output_csv}")
    else:
        print("No headers found to map.")

def main():
    passwords = get_passwords(config.PASS_FILE)
    import glob
    files = glob.glob(os.path.join(config.RAW_DIR, "*"))
    
    all_headers = {}
    
    for f in files:
        if not (f.endswith('.xls') or f.endswith('.xlsx')):
            continue
            
        print(f"Scanning {os.path.basename(f)}...")
        df = try_open_workbook(f, passwords)
        if df is None:
            print(f"Skipping {f} (Read Failed)")
            continue
            
        cleaned_df, _ = find_table_start(df)
        if cleaned_df is not None:
             all_headers[os.path.basename(f)] = cleaned_df.columns.tolist()
             
    generate_column_mapping(all_headers, config.MAPPING_FILE)

if __name__ == "__main__":
    main()
