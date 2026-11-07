"""
Raw statements -> one consolidated ledger (mega_statement.csv).

For every raw file: extract the transaction table, map its columns onto the
standard schema, clean amounts, normalise row order to oldest-first, and tag
it with its bank. Then concatenate everything, report and drop exact
duplicates (overlapping statements), and write the ledger.
"""
import glob
import hashlib
import os
import re

import pandas as pd

import config
from lib.parser import (find_table_end, find_table_start, get_passwords,
                        remove_empty_junk_columns, remove_junk_rows,
                        remove_leading_empty_columns, try_open_workbook)

STATEMENT_EXTENSIONS = ('.xls', '.xlsx', '.csv')
MONEY_COLUMNS = ["Debit", "Credit", "Balance"]

# Whole-value transaction types for "Amount + Type" statements, on top of the
# word-bounded credit/cr/debit/dr match.
CREDIT_TYPES = {"c", "cr", "cr.", "credit", "deposit"}
DEBIT_TYPES = {"d", "dr", "dr.", "debit", "withdrawal"}


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------

def get_file_mapping(mapping_df, filename):
    file_map = mapping_df[mapping_df['FileName'] == filename]
    if file_map.empty:
        return {}

    valid_map = file_map.dropna(subset=['MappedTo'])
    valid_map = valid_map[valid_map['MappedTo'] != ""]
    return dict(zip(valid_map['RawColumn'], valid_map['MappedTo']))


def load_bank_registry():
    """Bank name -> {Owner, Tags, ...} from bank_names.csv."""
    if not os.path.exists(config.BANKS_FILE):
        return {}
    try:
        return pd.read_csv(config.BANKS_FILE).fillna("").set_index('Name').to_dict(orient='index')
    except Exception:
        return {}


def load_file_to_bank(mapping_df):
    """
    Persisted per-file bank assignment (from column_mapping.csv, written during
    upload). This is the only durable record of the bank for files uploaded
    under a custom "Other" name that never made it into the registry -- without
    this fallback, re-running the pipeline (which reprocesses every raw file)
    would silently reclassify those files as "Unknown".
    """
    if 'Bank' not in mapping_df.columns or 'FileName' not in mapping_df.columns:
        return {}
    known = mapping_df.dropna(subset=['Bank'])
    known = known[known['Bank'] != ""]
    return known.drop_duplicates('FileName').set_index('FileName')['Bank'].to_dict()


def resolve_bank(basename, bank_overrides, banks_registry, file_to_bank):
    """Explicit override > registry name found in the filename > recorded bank."""
    if bank_overrides and basename in bank_overrides:
        return bank_overrides[basename]
    base_upper = basename.upper()
    for known_name in banks_registry:
        if str(known_name).upper() in base_upper:
            return known_name
    return file_to_bank.get(basename, "Unknown")


def generate_txn_id(row):
    s = f"{row.get('Date')}{row.get('Description')}{row.get('Debit')}{row.get('Credit')}{row.get('Balance')}{row.get('RefNumber')}"
    return hashlib.md5(s.encode('utf-8')).hexdigest()


# --------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------

def clean_description(parts):
    """Join the mapped description columns into one single-spaced line."""
    text = " ".join(parts.fillna('').astype(str))
    text = text.replace('\n', '').replace('\r', '').replace('\t', ' ')
    return " ".join(text.split()).strip()


def parse_amount(value):
    """First number in a value, ignoring thousands separators; 0.0 if none."""
    match = re.search(r'\d+\.?\d*', str(value).replace(',', ''))
    return float(match.group(0)) if match else 0.0


def clean_currency(val, is_balance=False):
    """
    Parse an amount cell like "1,234.50 Cr". A "Dr" suffix only flips the sign
    of a *balance* (an overdrawn account); on a debit column it's just a label.
    """
    if pd.isna(val):
        return 0.0
    s = str(val).strip().lower()
    if not s:
        return 0.0

    sign = -1.0 if (is_balance and 'dr' in s) else 1.0
    clean_s = s.replace(',', '').replace('cr', '').replace('dr', '').replace(' ', '')
    match = re.search(r'-?\d+\.?\d*', clean_s)
    if not match:
        return 0.0
    try:
        return float(match.group(0)) * sign
    except ValueError:
        return 0.0


def is_descending(df):
    """
    Whether a statement lists newest rows first.

    Balance arithmetic between the first two rows is decisive (and works for
    same-day rows, where dates can't tell); the date trend is the fallback.
    """
    if len(df) <= 1:
        return False

    valid_dates = df['Date'].dropna()
    date_desc = len(valid_dates) > 1 and valid_dates.iloc[0] > valid_dates.iloc[-1]

    try:
        r0, r1 = df.iloc[0], df.iloc[1]
        # Ascending: row 0 precedes row 1. Descending: row 1 precedes row 0.
        diff_asc = abs((r0['Balance'] + r1['Credit'] - r1['Debit']) - r1['Balance'])
        diff_desc = abs((r1['Balance'] + r0['Credit'] - r0['Debit']) - r0['Balance'])
        if diff_desc < diff_asc and diff_desc < 1.0:
            return True
        # Fall back to the date trend when the ascending math doesn't hold.
        return bool(date_desc and diff_asc > 1.0)
    except Exception:
        return bool(date_desc)


# --------------------------------------------------------------------------
# One file
# --------------------------------------------------------------------------

def extract_table(path, passwords):
    """
    The transaction table from a raw statement, or None if it can't be opened.
    The extracted table is also saved, so reconciliation can see the file's
    full original date range later.
    """
    df = try_open_workbook(path, passwords)
    if df is None:
        return None

    df = remove_leading_empty_columns(df)
    table, _ = find_table_start(df)
    table = find_table_end(table)
    table = remove_junk_rows(table)
    table = remove_empty_junk_columns(table)

    os.makedirs(config.EXTRACTION_DIR, exist_ok=True)
    table.to_csv(os.path.join(config.EXTRACTION_DIR, f"{os.path.basename(path)}.csv"), index=False)

    table.columns = [str(c) for c in table.columns]
    return table


def _standard_to_raw(col_map, columns):
    """{standard field: [raw columns]} for the mapped columns actually present."""
    std_to_raw = {}
    for raw, std in col_map.items():
        if raw in columns:
            std_to_raw.setdefault(std, []).append(raw)
    return std_to_raw


def _map_dates(table, date_cols, out):
    """
    The first mapped date column is authoritative (day-first: Indian banks).
    Any other mapped date column (value date, say) that disagrees is noted in
    SystemRemarks rather than dropped.
    """
    if not date_cols:
        out['Date'] = pd.NaT
        return
    out['Date'] = pd.to_datetime(table[date_cols[0]], errors='coerce', dayfirst=True)
    for other in date_cols[1:]:
        other_dates = pd.to_datetime(table[other], errors='coerce', dayfirst=True)
        differs = (out['Date'] != other_dates) & out['Date'].notna() & other_dates.notna()
        if differs.any():
            note = other + ": " + other_dates[differs].dt.strftime('%Y-%m-%d')
            out.loc[differs, 'SystemRemarks'] = out.loc[differs, 'SystemRemarks'] + note + "; "


def _map_amounts(table, std_to_raw, out):
    for col in MONEY_COLUMNS + ["RefNumber"]:
        raw_cols = std_to_raw.get(col, [])
        if raw_cols:
            out[col] = table[raw_cols[0]]
        else:
            out[col] = 0 if col in MONEY_COLUMNS else ""

    # "Amount + Type" statements: one amount column, direction in another.
    amt_cols = std_to_raw.get('Amount', [])
    type_cols = std_to_raw.get('TxnType', [])
    if amt_cols and type_cols:
        amounts = table[amt_cols[0]].apply(parse_amount)
        kind = table[type_cols[0]].astype(str).str.lower()
        # Word-bounded: a bare contains('c')/('d') used to match almost any
        # type value ("Card", "Cash", "Chq Deposit") and misclassify rows.
        whole = kind.str.strip()
        is_credit = kind.str.contains(r'\b(?:credit|cr)\b', regex=True) | whole.isin(CREDIT_TYPES)
        is_debit = kind.str.contains(r'\b(?:debit|dr)\b', regex=True) | whole.isin(DEBIT_TYPES)
        out.loc[is_debit, 'Debit'] = amounts[is_debit]
        out.loc[is_credit, 'Credit'] = amounts[is_credit]

        # An amount whose direction can't be read would otherwise become a
        # silent zero; flag it so it shows up for review.
        unknown = ~(is_credit | is_debit) & (amounts != 0)
        if unknown.any():
            raw_type = table[type_cols[0]].astype(str).str.strip()
            out.loc[unknown, 'SystemRemarks'] = (out.loc[unknown, 'SystemRemarks']
                                                 + "Unrecognised transaction type '" + raw_type[unknown] + "'; ")

    for col in MONEY_COLUMNS:
        is_balance = col == "Balance"
        out[col] = out[col].apply(lambda x: clean_currency(x, is_balance=is_balance))


def normalize_statement(table, col_map, basename):
    """Map one extracted table onto the standard ledger schema."""
    std_to_raw = _standard_to_raw(col_map, table.columns)

    out = pd.DataFrame(index=table.index)
    out['SourceFile'] = basename
    out['SystemRemarks'] = ""

    _map_dates(table, std_to_raw.get('Date', []), out)

    desc_cols = std_to_raw.get('Description', [])
    out['Description'] = table[desc_cols].apply(clean_description, axis=1) if desc_cols else ""

    _map_amounts(table, std_to_raw, out)
    out = out[~((out['Debit'] == 0) & (out['Credit'] == 0) & (out['Balance'] == 0))]

    # SortIndex makes every file oldest-first, so same-day rows keep their
    # true order once statements are merged.
    n = len(out)
    out['SortIndex'] = range(n - 1, -1, -1) if is_descending(out) else range(n)
    out['OriginalIndex'] = out.index
    return out


# --------------------------------------------------------------------------
# All files
# --------------------------------------------------------------------------

def _number_repeats(mega_df):
    """
    Give genuinely repeated transactions *within one file* distinct IDs.

    Without a running balance (wallet exports) two identical purchases on the
    same day hash to the same TxnID, and dedup deleted one of them. Numbering
    the 2nd, 3rd... occurrence per file keeps them, while the same rows coming
    from an overlapping file still get matching IDs and are deduplicated.
    The first occurrence keeps its plain ID, so existing IDs don't change.
    """
    occurrence = mega_df.groupby(['SourceFile', 'TxnID']).cumcount()
    return mega_df['TxnID'].where(occurrence == 0, mega_df['TxnID'] + '#' + occurrence.astype(str))


def write_duplicate_report(mega_df):
    """Every TxnID that appears more than once, first copy Kept, rest Deleted."""
    dupes = mega_df[mega_df.duplicated(subset=['TxnID'], keep=False)]
    if dupes.empty:
        return

    report = []
    for _, group in dupes.groupby('TxnID'):
        g = group.copy()
        g['DuplicateStatus'] = 'Deleted'
        g.iloc[0, g.columns.get_loc('DuplicateStatus')] = 'Kept'
        cols = ['DuplicateStatus'] + [c for c in g.columns if c != 'DuplicateStatus']
        report.append(g[cols])
        report.append(pd.DataFrame([[''] * len(cols)], columns=cols))  # spacer row
    pd.concat(report, ignore_index=True).to_csv(config.DUPLICATES_REPORT_CSV, index=False)


def run_pipeline(custom_files=None, bank_overrides=None):
    if not os.path.exists(config.MAPPING_FILE):
        return {"error": "Mapping file not found! Please run src/generate_mapping.py first."}

    mapping_df = pd.read_csv(config.MAPPING_FILE)
    passwords = get_passwords(config.PASS_FILE)
    banks_registry = load_bank_registry()
    file_to_bank = load_file_to_bank(mapping_df)

    summary = {
        "files_processed": 0,
        "files_skipped": 0,
        "total_rows_before_dedup": 0,
        "total_rows_after_dedup": 0,
        "bank_counts": {}
    }

    all_txns = []
    files = custom_files if custom_files else glob.glob(os.path.join(config.RAW_DIR, "*"))
    for path in files:
        if not path.endswith(STATEMENT_EXTENSIONS):
            continue

        basename = os.path.basename(path)
        col_map = get_file_mapping(mapping_df, basename)
        if not col_map:
            print(f"No mapping found for {basename}, skipping...")
            summary["files_skipped"] += 1
            continue

        print(f"Processing {basename}...")
        table = extract_table(path, passwords)
        if table is None:
            print(f"Failed to open {basename}")
            summary["files_skipped"] += 1
            continue

        statement = normalize_statement(table, col_map, basename)

        bank_name = resolve_bank(basename, bank_overrides, banks_registry, file_to_bank)
        meta = banks_registry.get(bank_name, {})
        statement['Bank'] = bank_name
        statement['Owner'] = meta.get('Owner', '')
        statement['Tags'] = meta.get('Tags', '')

        summary["bank_counts"][bank_name] = summary["bank_counts"].get(bank_name, 0) + 1
        summary["files_processed"] += 1
        all_txns.append(statement)

    if not all_txns:
        return {"error": "No transactions processed."}

    mega_df = pd.concat(all_txns, ignore_index=True)
    summary["total_rows_before_dedup"] = len(mega_df)

    # Date, then each file's normalised order, then bank.
    mega_df.sort_values(by=['Date', 'SortIndex', 'Bank'], inplace=True)
    mega_df['TxnID'] = mega_df.apply(generate_txn_id, axis=1)
    mega_df['TxnID'] = _number_repeats(mega_df)

    write_duplicate_report(mega_df)
    mega_df.drop_duplicates(subset=['TxnID'], inplace=True)
    summary["total_rows_after_dedup"] = len(mega_df)

    os.makedirs(config.ENRICHED_DIR, exist_ok=True)
    mega_df.to_csv(config.MEGA_STATEMENT_CSV, index=False)
    return summary


if __name__ == "__main__":
    print(run_pipeline())
