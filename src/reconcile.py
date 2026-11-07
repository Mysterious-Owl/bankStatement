import numpy as np
import pandas as pd
import config
import os

# A single bank statement should never legitimately span more than one financial
# year (365/366 days). A file that does is almost always several periods that got
# concatenated together (or a mis-mapped date column) and is worth a manual look.
MAX_STATEMENT_DAYS = 366

# Minimum length (in days) of a transaction-free stretch *inside* a single file
# before we bother surfacing it as a "quiet period". Below this, it's normal
# account inactivity (weekends, a slow month) and just adds noise.
QUIET_PERIOD_MIN_DAYS = 21


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

TOLERANCE = 1.0


def _ledger_bounds(df):
    """
    Opening balance before the first day and closing balance after the last.

    Same-day rows can come out of a merged ledger in any order, so the "first"
    and "last" row can't simply be read off a date sort -- that made a
    consistent bank flip to inconsistent depending on row order. Instead,
    within the first (last) day, pick the row whose implied opening
    (balance) is consistent with the whole day's net movement. Falls back to
    the positional first/last row when nothing fits.
    """
    days = df.groupby(df['Date'].astype(str).str[:10], sort=True)
    first = days.get_group(min(days.groups))
    last = days.get_group(max(days.groups))

    def net(day):
        return day['Credit'].sum() - day['Debit'].sum()

    openings = (first['Balance'] - first['Credit'] + first['Debit']).tolist()
    opening = next((o for o in openings
                    if any(abs(o + net(first) - b) <= TOLERANCE for b in first['Balance'])),
                   openings[0])

    last_openings = (last['Balance'] - last['Credit'] + last['Debit']).tolist()
    closing = next((b for b in last['Balance']
                    if any(abs(b - net(last) - o) <= TOLERANCE for o in last_openings)),
                   last['Balance'].iloc[-1])
    return opening, closing


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

    df = df.sort_values('Date', kind='mergesort')

    if df.empty:
        return {"Name": bank_name, "Consistency": "Empty"}

    opening_bal, closing_bal = _ledger_bounds(df)

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


def find_coverage_gaps(files_info):
    """
    Given a bank's files_info (each with start_date/end_date as YYYY-MM-DD
    strings), finds date ranges that NO file covers at all -- periods where we
    have zero documentation, so we genuinely can't tell if transactions happened.
    These are real "go get the missing statement" gaps, unlike quiet periods.
    """
    intervals = []
    for f in files_info:
        if f['start_date'] == '-' or f['end_date'] == '-':
            continue
        intervals.append([pd.to_datetime(f['start_date']), pd.to_datetime(f['end_date'])])

    if len(intervals) < 2:
        return []

    intervals.sort(key=lambda iv: iv[0])
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        if start <= merged[-1][1] + pd.Timedelta(days=1):
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])

    gaps = []
    for i in range(len(merged) - 1):
        gap_start = merged[i][1] + pd.Timedelta(days=1)
        gap_end = merged[i + 1][0] - pd.Timedelta(days=1)
        days = (gap_end - gap_start).days + 1
        if days >= 1:
            gaps.append({
                "start": gap_start.strftime('%Y-%m-%d'),
                "end": gap_end.strftime('%Y-%m-%d'),
                "days": int(days)
            })
    return gaps


def find_quiet_periods(dates):
    """
    dates: sorted list of unique transaction dates (pd.Timestamp) *within one
    file*. Finds internal stretches with no transactions. Because these sit
    inside a single statement's own start/end range, the file itself is proof
    we have documentation for that period -- this is "no activity", not
    "missing data" (a statement can't have a hole in the middle of it).
    """
    if len(dates) < 2:
        return []

    gaps = []
    for i in range(len(dates) - 1):
        d1, d2 = dates[i], dates[i + 1]
        gap_days = (d2 - d1).days - 1
        if gap_days >= QUIET_PERIOD_MIN_DAYS:
            gaps.append({
                "start": (d1 + pd.Timedelta(days=1)).strftime('%Y-%m-%d'),
                "end": (d2 - pd.Timedelta(days=1)).strftime('%Y-%m-%d'),
                "days": int(gap_days)
            })
    return gaps


def get_mapping_lookup():
    """
    Returns {filename: {raw_column: mapped_standard_column}} from
    column_mapping.csv, so we can find which raw column holds the date for a
    given file and read it straight from its raw extracted CSV.
    """
    if not os.path.exists(config.MAPPING_FILE):
        return {}
    try:
        mapping_df = pd.read_csv(config.MAPPING_FILE).fillna('')
    except Exception:
        return {}

    lookup = {}
    for _, row in mapping_df.iterrows():
        fname = row.get('FileName')
        if not fname:
            continue
        lookup.setdefault(fname, {})[row.get('RawColumn')] = row.get('MappedTo')
    return lookup


def get_raw_date_range(filename, mapping_lookup):
    """
    The full original date range of a raw extracted file, before any dedup or
    zero-amount filtering. A file that lost most of its rows to deduplication
    (e.g. 89 raw rows -> 1 kept) would otherwise show a start/end range
    collapsed to whatever the 1 surviving row's date happens to be -- wildly
    understating how much of the statement is actually duplicate coverage
    rather than genuinely short-lived. Returns (start, end) as YYYY-MM-DD
    strings, or (None, None) if it can't be determined.
    """
    raw_path = os.path.join(config.EXTRACTION_DIR, f"{filename}.csv")
    if not os.path.exists(raw_path):
        return None, None

    date_cols = [raw for raw, std in mapping_lookup.get(filename, {}).items() if std == 'Date']
    if not date_cols:
        return None, None

    try:
        raw_df = pd.read_csv(raw_path)
    except Exception:
        return None, None

    primary_date_col = date_cols[0]
    if primary_date_col not in raw_df.columns:
        return None, None

    dates = pd.to_datetime(raw_df[primary_date_col], errors='coerce', dayfirst=True).dropna()
    if dates.empty:
        return None, None

    return dates.min().strftime('%Y-%m-%d'), dates.max().strftime('%Y-%m-%d')


def analyze_duplicate_report():
    """
    Parses duplicates_report.csv into structures that let us tell WHICH raw
    files a given file's duplicate rows actually came from, so we can point at
    a specific redundant file rather than just a count.

    Returns:
      dup_pair_counts: {(deleted_file, kept_file): shared_txn_count}
      deleted_file_totals: {deleted_file: total_rows_deleted_as_duplicate}
      deleted_file_bank: {deleted_file: bank_name}
    """
    dup_pair_counts = {}
    deleted_file_totals = {}
    deleted_file_bank = {}

    if not os.path.exists(config.DUPLICATES_REPORT_CSV):
        return dup_pair_counts, deleted_file_totals, deleted_file_bank

    try:
        report = pd.read_csv(config.DUPLICATES_REPORT_CSV)
    except Exception:
        return dup_pair_counts, deleted_file_totals, deleted_file_bank

    if 'TxnID' not in report.columns or report.empty:
        return dup_pair_counts, deleted_file_totals, deleted_file_bank

    # The report has blank separator rows between duplicate groups (see
    # pipeline.run_pipeline) -- drop them.
    report = report.dropna(subset=['TxnID'])
    if report.empty:
        return dup_pair_counts, deleted_file_totals, deleted_file_bank

    kept_map = {}
    for txn_id, grp in report.groupby('TxnID'):
        kept_rows = grp[grp['DuplicateStatus'] == 'Kept']
        if not kept_rows.empty:
            kept_map[txn_id] = kept_rows.iloc[0]['SourceFile']

    deleted_rows = report[report['DuplicateStatus'] == 'Deleted']
    for _, row in deleted_rows.iterrows():
        deleted_file = row['SourceFile']
        deleted_file_totals[deleted_file] = deleted_file_totals.get(deleted_file, 0) + 1
        deleted_file_bank[deleted_file] = row.get('Bank', 'Unknown')

        kept_file = kept_map.get(row['TxnID'])
        if kept_file:
            key = (deleted_file, kept_file)
            dup_pair_counts[key] = dup_pair_counts.get(key, 0) + 1

    return dup_pair_counts, deleted_file_totals, deleted_file_bank


def to_native(value):
    """
    Recursively convert numpy scalars to plain Python types.

    Pandas aggregates come back as numpy scalars, and FastAPI's JSON encoder
    cannot serialize numpy.int64 -- so this endpoint returned a 500 whenever
    every amount in the ledger happened to be a whole number (which makes the
    Credit/Debit columns integer dtype rather than float).
    """
    if isinstance(value, dict):
        return {k: to_native(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_native(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def run_reconciliation():
    if not os.path.exists(config.MEGA_STATEMENT_CSV):
        return {"error": "Merged data not found. Run pipeline first."}

    df = pd.read_csv(config.MEGA_STATEMENT_CSV)

    # True per-file duplicate counts, from the dedup report (rows dropped for having a
    # duplicate TxnID) -- NOT the same as raw_count - final_count, which also includes
    # rows legitimately dropped by the zero-amount filter and is not a duplicate count.
    dup_counts_by_file = {}
    if os.path.exists(config.DUPLICATES_REPORT_CSV):
        try:
            dupes_report = pd.read_csv(config.DUPLICATES_REPORT_CSV)
            deleted = dupes_report[dupes_report['DuplicateStatus'] == 'Deleted']
            dup_counts_by_file = deleted.groupby('SourceFile').size().to_dict()
        except Exception:
            pass

    dup_pair_counts, deleted_file_totals, deleted_file_bank = analyze_duplicate_report()
    mapping_lookup = get_mapping_lookup()

    # Files with surviving rows in the ledger right now, keyed by filename.
    surviving_files = set(df['SourceFile'].unique())

    # Fully-redundant files: EVERY row they contributed was a duplicate of a row
    # kept from another file, so they add zero unique data to the ledger. These
    # are pure clutter in raw_statements/ and are safe to delete outright.
    fully_redundant_by_bank = {}
    for deleted_file, total in deleted_file_totals.items():
        if deleted_file in surviving_files:
            continue  # this file still contributes at least some unique rows
        bank = deleted_file_bank.get(deleted_file, 'Unknown')
        partners = {}
        for (d_name, kept_file), count in dup_pair_counts.items():
            if d_name == deleted_file:
                partners[kept_file] = partners.get(kept_file, 0) + count
        fully_redundant_by_bank.setdefault(bank, []).append({
            "filename": deleted_file,
            "duplicate_rows": int(total),
            "duplicate_of": [
                {"filename": k, "count": int(v)}
                for k, v in sorted(partners.items(), key=lambda kv: -kv[1])
            ]
        })

    # Pre-calculate bank level aggregates
    bank_level = []
    for bank, group in df.groupby('Bank'):
        res = check_bank_aggregate(group, bank)

        # Add file details for this bank
        files_info = []
        for filename, f_group in group.groupby('SourceFile'):
            # Get Raw Count from Extraction Dir if available
            raw_count = len(f_group) # Fallback
            raw_path = os.path.join(config.EXTRACTION_DIR, f"{filename}.csv")
            if os.path.exists(raw_path):
                try:
                    raw_df = pd.read_csv(raw_path)
                    raw_count = len(raw_df)
                except: pass

            final_count = len(f_group)

            # Date Range for this specific file (the range actually considered
            # / kept in the ledger)
            f_group = f_group.copy()
            f_group['Date'] = pd.to_datetime(f_group['Date'], errors='coerce')
            f_dates = f_group['Date'].dropna().sort_values()
            f_min = f_dates.iloc[0].strftime('%Y-%m-%d') if not f_dates.empty else "-"
            f_max = f_dates.iloc[-1].strftime('%Y-%m-%d') if not f_dates.empty else "-"
            span_days = int((f_dates.iloc[-1] - f_dates.iloc[0]).days) if not f_dates.empty else 0

            # The file's original, full date range before any dedup/filtering --
            # lets the UI show how much of the statement's real coverage is
            # hidden behind rows that got dropped as duplicates elsewhere.
            raw_start, raw_end = get_raw_date_range(filename, mapping_lookup)

            # NOTE: use a distinct variable name here -- reusing `res` previously
            # clobbered the bank-level result from check_bank_aggregate() above with
            # the last file's math-check result once this loop finished.
            file_res = check_file_math(f_group, filename)

            # Which other file(s) does this file share duplicate transactions
            # with? Lets the UI point at a specific partner instead of just a count.
            duplicate_partners = {}
            for (d_name, kept_file), count in dup_pair_counts.items():
                other = None
                if d_name == filename:
                    other = kept_file
                elif kept_file == filename:
                    other = d_name
                if other and other != filename:
                    duplicate_partners[other] = duplicate_partners.get(other, 0) + count

            files_info.append({
                "filename": filename,
                "raw_count": raw_count,
                "final_count": final_count,
                "duplicates": dup_counts_by_file.get(filename, 0),
                "start_date": f_min,
                "end_date": f_max,
                "span_days": span_days,
                "raw_start_date": raw_start,
                "raw_end_date": raw_end,
                "is_long_statement": span_days > MAX_STATEMENT_DAYS,
                "quiet_periods": find_quiet_periods(list(f_dates.unique())),
                "duplicate_partners": [
                    {"filename": k, "count": int(v)}
                    for k, v in sorted(duplicate_partners.items(), key=lambda kv: -kv[1])
                ],
                "TotalCredit": file_res.get("TotalCredit", 0),
                "TotalDebit": file_res.get("TotalDebit", 0),
                "Consistency": file_res.get("Consistency", "Unknown")
            })

        files_info.sort(key=lambda f: f['start_date'])

        res["Files"] = files_info
        res["CoverageGaps"] = find_coverage_gaps(files_info)
        res["RedundantFiles"] = fully_redundant_by_bank.pop(bank, [])

        # Bank overall dates
        group['Date'] = pd.to_datetime(group['Date'], errors='coerce')
        b_dates = group['Date'].dropna().sort_values()
        res["StartDate"] = b_dates.iloc[0].strftime('%Y-%m-%d') if not b_dates.empty else "-"
        res["EndDate"] = b_dates.iloc[-1].strftime('%Y-%m-%d') if not b_dates.empty else "-"

        # Transaction count
        res["TotalTransactions"] = len(group)
        res["FileCount"] = len(files_info)

        bank_level.append(res)

    # Banks whose files are ALL fully-redundant duplicates never show up in
    # df.groupby('Bank') above (they have zero surviving rows) -- surface them
    # anyway so their raw files aren't invisible cleanup candidates.
    for bank, redundant_files in fully_redundant_by_bank.items():
        bank_level.append({
            "Name": f"All {bank} (Aggregate)",
            "TotalCredit": 0,
            "TotalDebit": 0,
            "Consistency": "N/A (No surviving transactions -- all files fully duplicated elsewhere)",
            "Files": [],
            "CoverageGaps": [],
            "RedundantFiles": redundant_files,
            "StartDate": "-",
            "EndDate": "-",
            "TotalTransactions": 0,
            "FileCount": 0
        })

    return to_native({
        "bank_level": bank_level
    })

if __name__ == "__main__":
    res = run_reconciliation()
    if "error" in res:
        print(res["error"])
    else:
        for bank in res["bank_level"]:
            print(f"Bank: {bank['Name']} ({bank['StartDate']} to {bank['EndDate']})")
            print(f"  Consistency: {bank['Consistency']}")
            print(f"  Files: {bank['FileCount']}, Transactions: {bank['TotalTransactions']}")
            for f in bank["Files"]:
                print(f"    - {f['filename']}: Raw={f['raw_count']}, Final={f['final_count']}, Dupes={f['duplicates']} ({f['start_date']} to {f['end_date']})")
            for gap in bank.get("CoverageGaps", []):
                print(f"    !! Missing coverage: {gap['start']} to {gap['end']} ({gap['days']} days)")
            for rf in bank.get("RedundantFiles", []):
                print(f"    ~~ Fully redundant, safe to delete: {rf['filename']} ({rf['duplicate_rows']} rows)")
