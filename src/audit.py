"""
Audit checks over the enriched ledger.

These answer "can I trust these numbers?" rather than "what do they say?":

* balance_continuity -- walks every bank's running balance day by day and
  reports exactly where (and by how much) the recorded transactions fail to
  explain the change in balance. A date gap only says data *might* be missing;
  a balance break says it *is*, and by how many rupees.
* near_duplicates -- the same transaction surviving twice because two exports
  of one period differed in some field the exact-hash dedup keys on.
* review_progress -- how much of the ledger (by rows and by rupees) a human
  has actually checked.

All functions take the enriched frame (see api.enrichment) and are pure, so
they can be tested against synthetic ledgers.
"""
import hashlib

import pandas as pd

import analytics
import annotations
import frames

# Rupee tolerance for balance arithmetic -- statements round to paise, and the
# existing per-file check in reconcile.py uses the same threshold.
BALANCE_TOLERANCE = 1.0


# --------------------------------------------------------------------------
# Balance continuity
# --------------------------------------------------------------------------

def _close(a, b):
    return abs(a - b) <= BALANCE_TOLERANCE


def _prepare(group):
    g = frames.with_amounts(group, balance=True)
    g['_day'] = g['Date'].dt.normalize()

    # SortIndex is only meaningful *within* one file, so files are ordered by
    # where they start; this is just a fallback for picking an anchor balance
    # after a break -- the check itself never depends on intra-day order.
    file_start = g.groupby('SourceFile')['Date'].min().rank(method='first')
    g['_file_order'] = g['SourceFile'].map(file_start)
    sort_cols = ['_day', '_file_order'] + (['SortIndex'] if 'SortIndex' in g.columns else [])
    return g.sort_values(sort_cols, kind='mergesort')


def _first_closing(rows, net, balances):
    """
    Closing balance of the first day, when there is no previous day to start
    from: try each row as the day's first transaction and keep the opening it
    implies if that opening plus the day's net lands on a recorded balance.
    """
    for _, row in rows.iterrows():
        if pd.isna(row['Balance']):
            continue
        opening = row['Balance'] - row['Credit'] + row['Debit']
        for b in balances:
            if _close(opening + net, b):
                return b, opening
    last = rows['Balance'].dropna()
    if last.empty:
        return None, None
    closing = last.iloc[-1]
    return closing, closing - net


def _anchor_after_break(rows, balances, next_day):
    """
    After a break the true closing balance is unknown; pick the recorded
    balance that makes the *next* day reconcile, falling back to the last row.
    """
    if next_day is not None:
        next_net = next_day['Credit'].sum() - next_day['Debit'].sum()
        next_balances = next_day['Balance'].dropna().tolist()
        for b in balances:
            if any(_close(b + next_net, nb) for nb in next_balances):
                return b
    return rows['Balance'].dropna().iloc[-1]


def _walk_bank(bank, group):
    g = _prepare(group)
    summary = {
        "bank": bank,
        "status": "continuous",
        "days_checked": 0,
        "breaks": [],
        "seam_breaks": 0,
        "unexplained_total": 0.0,
        "first_date": None,
        "last_date": None,
        "opening_balance": None,
        "closing_balance": None,
    }
    if g.empty:
        summary["status"] = "empty"
        return summary

    summary["first_date"] = g['Date'].min().strftime('%Y-%m-%d')
    summary["last_date"] = g['Date'].max().strftime('%Y-%m-%d')

    # Some exports carry no running balance at all (the column is mapped to
    # nothing and filled with zeros). Nothing can be verified for those.
    if g['Balance'].fillna(0).abs().max() < 0.005:
        summary["status"] = "no_balance"
        return summary

    days = [(day, rows) for day, rows in g.groupby('_day', sort=True)]
    closing = None
    prev_day = prev_rows = None

    for i, (day, rows) in enumerate(days):
        net = rows['Credit'].sum() - rows['Debit'].sum()
        balances = rows['Balance'].dropna().tolist()
        next_rows = days[i + 1][1] if i + 1 < len(days) else None

        if closing is None:
            closing, opening = _first_closing(rows, net, balances)
            if closing is not None:
                summary["opening_balance"] = round(float(opening), 2)
                prev_day, prev_rows = day, rows
            continue

        expected = closing + net
        if not balances:
            # No balance recorded today -- carry the arithmetic forward.
            closing = expected
            prev_day, prev_rows = day, rows
            continue

        summary["days_checked"] += 1

        # Order-independent: the day reconciles if the expected closing is
        # *any* of the balances recorded that day. Requiring it to be the last
        # row's balance would flag every day whose same-day rows came out of
        # the export in a different order -- which is routine.
        match = next((b for b in balances if _close(b, expected)), None)
        if match is not None:
            closing = match
            prev_day, prev_rows = day, rows
            continue

        actual = _anchor_after_break(rows, balances, next_rows)
        from_file = str(prev_rows['SourceFile'].iloc[-1])
        to_file = str(rows['SourceFile'].iloc[0])
        shared_files = set(prev_rows['SourceFile']) & set(rows['SourceFile'])
        kind = "seam" if not shared_files and from_file != to_file else "within_file"

        unexplained = round(float(actual - expected), 2)
        summary["breaks"].append({
            "kind": kind,
            "from_date": prev_day.strftime('%Y-%m-%d'),
            "to_date": day.strftime('%Y-%m-%d'),
            "gap_days": int((day - prev_day).days),
            "from_file": from_file,
            "to_file": to_file,
            "previous_closing": round(float(closing), 2),
            "expected_balance": round(float(expected), 2),
            "actual_balance": round(float(actual), 2),
            # Positive: the balance rose by more than the recorded transactions
            # explain, i.e. credits are missing (or debits were recorded that
            # never happened). Negative: debits are missing.
            "unexplained": unexplained,
            "transactions_on_day": int(len(rows)),
        })
        summary["unexplained_total"] += abs(unexplained)
        if kind == "seam":
            summary["seam_breaks"] += 1
        closing = actual
        prev_day, prev_rows = day, rows

    summary["closing_balance"] = round(float(closing), 2) if closing is not None else None
    summary["unexplained_total"] = round(summary["unexplained_total"], 2)
    if summary["breaks"]:
        summary["status"] = "breaks"
    return summary


def balance_continuity(df):
    """
    Per-bank running-balance walk.

    Rows the user has ignored are left out: an ignored row is a duplicate copy,
    not a real movement of money, and counting it would report its amount as a
    balance break forever. (A surviving duplicate that has *not* been ignored
    shows up here as a break on its day -- which is correct, and is how the
    near-duplicate report and this one corroborate each other.)
    """
    if df.empty:
        return []
    df = frames.live_rows(df)
    return [_walk_bank(bank, group) for bank, group in df.groupby('Bank')]


# --------------------------------------------------------------------------
# Near duplicates
# --------------------------------------------------------------------------

def _group_id(bank, day, amount, files):
    raw = f"{bank}|{day}|{amount:.2f}|{'|'.join(sorted(files))}"
    return hashlib.md5(raw.encode('utf-8')).hexdigest()[:16]


def near_duplicates(df, dismissed=()):
    """
    Same bank, same day, same signed amount, in two or more *different* files.

    The exact-hash dedup in the pipeline keys on the raw description and
    reference number, so two exports of one period that format either
    differently both survive. Requiring different source files is what keeps
    this from flagging two genuine identical purchases on the same statement.

    Confidence is "high" when every row has a counterpart in another file with
    the same running balance -- a balance is a near-unique fingerprint of a
    position in the account's history -- and "medium" otherwise.
    """
    if df.empty:
        return []

    work = frames.with_amounts(df)
    work['_signed'] = (work['Credit'] - work['Debit']).round(2)
    work = work[work['_signed'] != 0]
    work['_day'] = work['Date'].dt.strftime('%Y-%m-%d')

    dismissed = set(dismissed)
    groups = []
    for (bank, day, amount), rows in work.groupby(['Bank', '_day', '_signed']):
        files = set(rows['SourceFile'])
        if len(rows) < 2 or len(files) < 2:
            continue

        group_id = _group_id(bank, day, amount, files)
        if group_id in dismissed:
            continue

        ignored = frames.bool_column(rows, 'is_ignored')
        # Resolved once the live rows all come from one file -- the user has
        # ignored the copies that came in through the other export(s).
        live_per_file = rows[~ignored].groupby('SourceFile').size()
        if len(live_per_file) <= 1:
            continue
        # The file holding the most copies is taken to hold all the genuine
        # transactions; every live row beyond that is presumed double-counted.
        extra_copies = int(live_per_file.sum() - live_per_file.max())

        balances = frames.amount(rows['Balance'], fill=None)
        high = True
        for idx, row in rows.iterrows():
            others = balances[(rows['SourceFile'] != row['SourceFile'])]
            if pd.isna(balances[idx]) or not (others - balances[idx]).abs().le(BALANCE_TOLERANCE).any():
                high = False
                break

        groups.append({
            "group_id": group_id,
            "bank": bank,
            "date": day,
            "amount": float(amount),
            "confidence": "high" if high else "medium",
            "files": sorted(files),
            # How much the totals are inflated by if these really are copies.
            "overstatement": round(abs(float(amount)) * extra_copies, 2),
            "rows": [
                {
                    "row_key": annotations.row_key(row.get('anno_key'), row.get('SourceFile')),
                    "anno_key": row.get('anno_key'),
                    "txn_id": str(row.get('TxnID', '')),
                    "source_file": row.get('SourceFile'),
                    "description": str(row.get('Description', '')),
                    "ref_number": '' if pd.isna(row.get('RefNumber')) else str(row.get('RefNumber')),
                    "balance": None if pd.isna(balances[idx]) else float(balances[idx]),
                    "ignored": bool(ignored[idx]),
                }
                for idx, row in rows.iterrows()
            ],
        })

    groups.sort(key=lambda g: (g["confidence"] != "high", -abs(g["amount"])))
    return groups


# --------------------------------------------------------------------------
# Review progress
# --------------------------------------------------------------------------

def _largest_unreviewed(pending, amount, top_n):
    """
    The biggest unreviewed items, with look-alikes folded together.

    Without grouping, a ledger with a monthly salary shows twelve identical
    salary credits here and nothing else. Grouped by bank + payee stem +
    direction, one entry carries every matching key so a single click can
    review the lot -- and the next-largest *different* thing becomes visible.
    """
    if pending.empty:
        return []
    work = pending.assign(_amount=amount)
    work['_group'] = [
        (row.get('Bank'), analytics.description_stem(row.get('Description')) or str(row.get('Description', '')),
         'out' if float(row.get('Debit') or 0) > 0 else 'in')
        for _, row in work.iterrows()
    ]

    groups = []
    for _, rows in work.groupby('_group', sort=False):
        rows = rows.sort_values('_amount', ascending=False)
        top = rows.iloc[0]
        groups.append({
            "anno_key": top.get('anno_key'),
            # every unreviewed key in the group; duplicates collapse because
            # review state is per anno_key
            "keys": sorted({str(k) for k in rows['anno_key'] if pd.notna(k)}),
            "count": int(len(rows)),
            "total": round(float(rows['_amount'].sum()), 2),
            "date": pd.to_datetime(top['Date']).strftime('%Y-%m-%d') if pd.notna(top['Date']) else '',
            "last_date": pd.to_datetime(rows['Date']).max().strftime('%Y-%m-%d'),
            "first_date": pd.to_datetime(rows['Date']).min().strftime('%Y-%m-%d'),
            "bank": top.get('Bank'),
            "description": str(top.get('Description', '')),
            "debit": float(top.get('Debit') or 0),
            "credit": float(top.get('Credit') or 0),
            "category": top.get('matched_category'),
            "_rank": float(top['_amount']),
        })

    groups.sort(key=lambda g: g["_rank"], reverse=True)
    for g in groups:
        del g["_rank"]
    return groups[:top_n]


def review_progress(df, top_n=15):
    """
    Share of the ledger a human has checked, by rows and by rupees.

    Rupees matter more than rows: fifty reviewed tea purchases say little
    about a ledger whose one unreviewed row is a two-lakh transfer.
    """
    empty = {"rows_total": 0, "rows_reviewed": 0, "spend_total": 0.0,
             "spend_reviewed": 0.0, "pct_rows": 0.0, "pct_spend": 0.0,
             "by_bank": [], "largest_unreviewed": []}
    if df.empty:
        return empty

    live = frames.live_rows(df)
    reviewed = frames.bool_column(live, 'is_reviewed')
    amount = frames.amount(live['Debit']) + frames.amount(live['Credit'])

    rows_total = int(len(live))
    rows_reviewed = int(reviewed.sum())
    spend_total = float(amount.sum())
    spend_reviewed = float(amount[reviewed].sum())

    by_bank = []
    for bank, idx in live.groupby('Bank').groups.items():
        r = reviewed.loc[idx]
        a = amount.loc[idx]
        by_bank.append({
            "bank": bank,
            "rows_total": int(len(idx)),
            "rows_reviewed": int(r.sum()),
            "pct_spend": round(100 * float(a[r].sum()) / float(a.sum()), 1) if a.sum() else 0.0,
        })

    largest = _largest_unreviewed(live[~reviewed], amount[~reviewed], top_n)

    return {
        "rows_total": rows_total,
        "rows_reviewed": rows_reviewed,
        "spend_total": round(spend_total, 2),
        "spend_reviewed": round(spend_reviewed, 2),
        "pct_rows": round(100 * rows_reviewed / rows_total, 1) if rows_total else 0.0,
        "pct_spend": round(100 * spend_reviewed / spend_total, 1) if spend_total else 0.0,
        "by_bank": sorted(by_bank, key=lambda b: b["bank"]),
        "largest_unreviewed": largest,
    }
