import pandas as pd
import config
import os

def check_file_math(df, name):
    """
    Checks if Balance[i] vs Balance[i-1] is consistent with Credit/Debit.
    """
    df = df.copy()
    # Ensure numeric
    cols = ['Credit', 'Debit', 'Balance']
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
        
    total_cr = df['Credit'].sum()
    total_dr = df['Debit'].sum()
    
    if len(df) < 2:
        return {
            "Name": name,
            "TotalCredit": total_cr,
            "TotalDebit": total_dr,
            "Consistency": "N/A (Too few rows)"
        }
        
    # Check 1: Ascending (Row i = Prev + Cr - Dr)
    df['PrevBal'] = df['Balance'].shift(1)
    df['Diff_Asc'] = df['Balance'] - (df['PrevBal'] + df['Credit'] - df['Debit'])
    match_asc = (df['Diff_Asc'].abs() < 0.01).sum() # Float tolerance
    
    # Check 2: Descending (Row i = Next + Cr - Dr) i.e. Newest top
    df['NextBal'] = df['Balance'].shift(-1)
    df['Diff_Desc'] = df['Balance'] - (df['NextBal'] + df['Credit'] - df['Debit'])
    match_desc = (df['Diff_Desc'].abs() < 0.01).sum()
    
    score_asc = match_asc / (len(df) - 1)
    score_desc = match_desc / (len(df) - 1)
    
    status = "Unknown"
    if score_asc > 0.9:
        status = f"Consistent (Ascending) {match_asc}/{len(df)-1}"
    elif score_desc > 0.9:
        status = f"Consistent (Descending) {match_desc}/{len(df)-1}"
    else:
        status = f"Inconsistent (Best: {max(match_asc, match_desc)}/{len(df)-1})"
        
    return {
        "Name": name,
        "TotalCredit": total_cr,
        "TotalDebit": total_dr,
        "Consistency": status
    }

def check_bank_aggregate(df, bank_name):
    """
    Checks overall consistency:
    Final Balance - Opening Balance == Total Credit - Total Debit
    
    Opening Balance is derived from the 'first' transaction:
    Opening = Bal_First - Cr_First + Dr_First
    """
    df = df.copy()
    cols = ['Credit', 'Debit', 'Balance']
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
        
    # Sort strictly by Date to find start/end bounds
    # Note: If same-day txns exist, simple date sort might fail to identify true 'first'/'last'
    # if the index isn't preserved. We use existing order if we assume it's roughly sorted?
    # No, for Bank Level (merged), we MUST sort by Date.
    # To handle same-day order, we can't do much without time.
    # But Aggregate Check doesn't care about internal order, ONLY correct First and Last rows.
    
    df = df.sort_values('Date')
    
    if df.empty:
        return {"Name": bank_name, "Consistency": "Empty"}
        
    first_row = df.iloc[0]
    last_row = df.iloc[-1]
    
    # Calculate inferred opening balance
    opening_bal = first_row['Balance'] - first_row['Credit'] + first_row['Debit']
    closing_bal = last_row['Balance']
    
    full_delta = closing_bal - opening_bal
    
    total_cr = df['Credit'].sum()
    total_dr = df['Debit'].sum()
    
    calc_delta = total_cr - total_dr
    
    diff = abs(full_delta - calc_delta)
    is_consistent = diff < 1.0 # Tolerance
    
    status_msg = f"Consistent (Delta Diff: {diff:.2f})" if is_consistent else f"Inconsistent (Exp Delta: {calc_delta:.2f}, Act Delta: {full_delta:.2f})"
    
    return {
        "Name": f"All {bank_name} (Aggregate)",
        "TotalCredit": total_cr,
        "TotalDebit": total_dr,
        "Consistency": status_msg
    }

def run_reconciliation():
    if not os.path.exists(config.MEGA_STATEMENT_PARQUET):
        return {"error": "Merged data not found. Run pipeline first."}
        
    df = pd.read_parquet(config.MEGA_STATEMENT_PARQUET)
    
    per_file = []
    # Group by SourceFile
    for filename, group in df.groupby('SourceFile'):
        res = check_file_math(group, filename)
        per_file.append(res)
        
    bank_level = []
    for bank, group in df.groupby('Bank'):
        res = check_bank_aggregate(group, bank)
        bank_level.append(res)
        
    return {
        "per_file": per_file,
        "bank_level": bank_level
    }

if __name__ == "__main__":
    res = run_reconciliation()
    print(res)
