"""
Spending analysis over the enriched ledger: budgets, recurring payments,
month-over-month trends, and how much spend is still uncategorized.

Every function works on *flow* rows only -- confirmed self transfers and rows
the user marked to ignore are excluded (see api.helpers.flow_rows) -- because
budgets and trends computed on internal transfers or duplicate copies would be
inflated by exactly the amounts that aren't real spending.

All functions are pure over the frame passed in, so they can be tested against
synthetic ledgers.
"""
import calendar
import re
from statistics import median

import pandas as pd

import frames

UNCATEGORIZED = 'Uncategorized'

# Extracted rule fields that name the counterparty, in order of preference.
PAYEE_FIELDS = ('upi_id', 'payee', 'merchant', 'vpa', 'beneficiary')

# Tokens too generic to identify a payee or anchor a suggested rule.
GENERIC_TOKENS = {
    'UPI', 'NEFT', 'IMPS', 'RTGS', 'POS', 'ATM', 'ACH', 'NACH', 'ECS', 'BIL', 'ONL',
    'TO', 'FROM', 'BY', 'FOR', 'THE', 'AND', 'TRANSFER', 'PAYMENT', 'PAY', 'TXN',
    'DR', 'CR', 'REF', 'INB', 'MB', 'IB', 'OTHERS', 'PVT', 'LTD',
}

# name -> (min days, max days) between occurrences
CADENCES = [
    ('weekly', 5, 9),
    ('fortnightly', 12, 17),
    ('monthly', 25, 35),
    ('quarterly', 80, 100),
    ('half-yearly', 170, 195),
    ('yearly', 350, 380),
]
# Occurrences per month for each cadence. Exact multipliers rather than
# 30.44 / interval: a monthly salary paid on 31-day gaps is still one salary a
# month, not 0.98 of one.
PER_MONTH = {
    'weekly': 52 / 12,
    'fortnightly': 26 / 12,
    'monthly': 1.0,
    'quarterly': 1 / 3,
    'half-yearly': 1 / 6,
    'yearly': 1 / 12,
}


# --------------------------------------------------------------------------
# Shared
# --------------------------------------------------------------------------

_BASE_COLUMNS = ['Date', 'Description', 'Debit', 'Credit', 'Bank', 'matched_category', 'tags', 'extracted_fields']


def _prepared(df):
    # Before the first upload there is no ledger at all and the enriched frame
    # has no columns; give it the expected shape so every caller can treat it
    # as an ordinary empty ledger.
    out = frames.with_amounts(df if 'Date' in df.columns else pd.DataFrame(columns=_BASE_COLUMNS))
    out['_month'] = out['Date'].dt.strftime('%Y-%m')
    if 'matched_category' not in out.columns:
        out['matched_category'] = UNCATEGORIZED
    out['matched_category'] = out['matched_category'].fillna(UNCATEGORIZED)
    return out


def _month_range(end_month, count):
    end = pd.Period(end_month, freq='M')
    return [str(end - i) for i in range(count - 1, -1, -1)]


def _days_in_month(month):
    year, mon = (int(x) for x in month.split('-'))
    return calendar.monthrange(year, mon)[1]


def elapsed_fraction(month, data_through):
    """
    How much of `month` the ledger actually covers.

    Based on the last date present in the ledger, not today's date: statements
    arrive late, and pacing against the calendar would make a month look
    underspent simply because its transactions haven't been uploaded yet.
    """
    if data_through is None:
        return 0.0
    period = pd.Period(month, freq='M')
    through = pd.Period(data_through, freq='D')
    if through < period.start_time.to_period('D'):
        return 0.0
    if through >= period.end_time.to_period('D'):
        return 1.0
    return through.day / _days_in_month(month)


def description_stem(description, tokens=4):
    """
    A payee-ish fingerprint of a free-text description: drop reference numbers
    and anything containing a digit, keep the first few meaningful tokens.
    """
    words = re.split(r'[^A-Za-z0-9@.]+', str(description or '').upper())
    kept = [w.strip('.') for w in words if w and not any(c.isdigit() for c in w)]
    kept = [w for w in kept if w and w not in GENERIC_TOKENS]
    return ' '.join(kept[:tokens])


def payee_of(row):
    extracted = row.get('extracted_fields')
    if isinstance(extracted, dict):
        for field in PAYEE_FIELDS:
            if extracted.get(field):
                return str(extracted[field]).strip().upper()
    return description_stem(row.get('Description'))


# --------------------------------------------------------------------------
# Uncategorized coverage
# --------------------------------------------------------------------------

def _suggest_pattern(stem):
    """The most distinctive token of a stem, as a regex a rule could start from."""
    candidates = [t for t in stem.split() if len(t) >= 3]
    if not candidates:
        return ''
    return re.escape(max(candidates, key=len))


def uncategorized_coverage(flow, start=None, end=None, top_n=12):
    """
    How much *spending* -- in rupees, not rows -- has no category.

    A row count misleads in both directions: fifty uncategorized tea purchases
    matter less than one uncategorized two-lakh payment. The top groups come
    with a suggested pattern so the fix is one click away on the rules page.
    """
    df = _prepared(flow)
    if start:
        df = df[df['Date'] >= pd.to_datetime(start)]
    if end:
        df = df[df['Date'] <= pd.to_datetime(end)]
    spend = df[df['Debit'] > 0]

    total = float(spend['Debit'].sum())
    uncat = spend[spend['matched_category'] == UNCATEGORIZED]
    uncat_total = float(uncat['Debit'].sum())

    top = []
    if not uncat.empty:
        stems = uncat.assign(_stem=uncat['Description'].map(description_stem))
        stems = stems[stems['_stem'] != '']
        grouped = stems.groupby('_stem').agg(
            total=('Debit', 'sum'), count=('Debit', 'size'),
            sample=('Description', 'first'), last=('Date', 'max'))
        for stem, row in grouped.sort_values('total', ascending=False).head(top_n).iterrows():
            top.append({
                "stem": stem,
                "total": round(float(row['total']), 2),
                "count": int(row['count']),
                "share": round(100 * float(row['total']) / uncat_total, 1) if uncat_total else 0.0,
                "sample": str(row['sample']),
                "last_seen": row['last'].strftime('%Y-%m-%d'),
                "suggested_pattern": _suggest_pattern(stem),
            })

    return {
        "spend_total": round(total, 2),
        "uncategorized_total": round(uncat_total, 2),
        "pct_spend": round(100 * uncat_total / total, 1) if total else 0.0,
        "rows_total": int(len(spend)),
        "rows_uncategorized": int(len(uncat)),
        "pct_rows": round(100 * len(uncat) / len(spend), 1) if len(spend) else 0.0,
        "top_groups": top,
    }


# --------------------------------------------------------------------------
# Trends
# --------------------------------------------------------------------------

def _pct_change(new, old):
    if not old:
        return None
    return round(100 * (new - old) / old, 1)


def monthly_trends(flow, months=12, category_colors=None):
    """
    Month-by-month income, spending and per-category spending.

    Comparisons are made on the latest *complete* month: a month the ledger
    only partly covers would always look like a sharp drop.
    """
    df = _prepared(flow)
    if df.empty:
        return {"months": [], "credit": [], "debit": [], "net": [], "categories": [],
                "reference_month": None, "latest_partial": False, "data_through": None}

    data_through = df['Date'].max()
    latest_month = data_through.strftime('%Y-%m')
    latest_partial = elapsed_fraction(latest_month, data_through) < 1.0
    window = _month_range(latest_month, months)

    totals = df.groupby('_month').agg(credit=('Credit', 'sum'), debit=('Debit', 'sum')).reindex(window, fill_value=0.0)

    reference = window[-2] if latest_partial and len(window) >= 2 else window[-1]
    ref_idx = window.index(reference)

    spend = df[df['Debit'] > 0]
    by_cat = spend.pivot_table(index='matched_category', columns='_month', values='Debit',
                               aggfunc='sum', fill_value=0.0).reindex(columns=window, fill_value=0.0)

    colors = category_colors or {}
    categories = []
    for name, series in by_cat.iterrows():
        values = [round(float(v), 2) for v in series.tolist()]
        ref = values[ref_idx]
        prev = values[ref_idx - 1] if ref_idx >= 1 else 0.0
        trailing = values[max(0, ref_idx - 3):ref_idx]
        avg3 = sum(trailing) / len(trailing) if trailing else 0.0
        if not any(values):
            continue
        categories.append({
            "name": name,
            "color": colors.get(name, '#8e8e93'),
            "series": values,
            "reference": ref,
            "previous": round(prev, 2),
            "avg3": round(avg3, 2),
            "change_vs_previous": _pct_change(ref, prev),
            "change_vs_avg3": _pct_change(ref, avg3),
            "window_total": round(sum(values), 2),
        })
    categories.sort(key=lambda c: c["reference"], reverse=True)

    return {
        "months": window,
        "credit": [round(float(v), 2) for v in totals['credit']],
        "debit": [round(float(v), 2) for v in totals['debit']],
        "net": [round(float(c - d), 2) for c, d in zip(totals['credit'], totals['debit'])],
        "categories": categories,
        "reference_month": reference,
        "latest_partial": latest_partial,
        "data_through": data_through.strftime('%Y-%m-%d'),
    }


# --------------------------------------------------------------------------
# Recurring payments
# --------------------------------------------------------------------------

def _classify_cadence(interval):
    for name, low, high in CADENCES:
        if low <= interval <= high:
            return name
    return None


def _price_changes(dates, amounts):
    """
    Step changes in a fixed-price series: an amount that differs by more than
    10% from the previous one *and* is confirmed by the next occurrence -- a
    one-off odd charge is noise, a new price that sticks is a change.
    """
    changes = []
    for i in range(1, len(amounts)):
        before, after = amounts[i - 1], amounts[i]
        if before <= 0 or abs(after - before) / before <= 0.10:
            continue
        if i + 1 < len(amounts) and abs(amounts[i + 1] - after) / after > 0.10:
            continue
        changes.append({"date": dates[i], "from": round(before, 2), "to": round(after, 2),
                        "pct": round(100 * (after - before) / before, 1)})
    return changes


def detect_recurring(flow, min_occurrences=3):
    """
    Payments to (or from) the same payee at a regular cadence.

    A payee qualifies when it appears on at least `min_occurrences` distinct
    days whose spacing falls in a known cadence band and is regular -- at least
    60% of gaps within 25% of the typical gap. Frequent but irregular payees
    (a food-delivery app, say) are deliberately excluded: they're habits, not
    commitments.
    """
    df = _prepared(flow)
    if df.empty:
        return {"items": [], "committed_monthly_out": 0.0, "recurring_monthly_in": 0.0,
                "data_through": None}

    data_through = df['Date'].max()
    df['_payee'] = [payee_of(row) for _, row in df.iterrows()]
    df = df[df['_payee'] != '']

    items = []
    for direction, column in (('out', 'Debit'), ('in', 'Credit')):
        side = df[df[column] > 0]
        for payee, rows in side.groupby('_payee'):
            # One occurrence per day: two charges on one day are one event for
            # cadence purposes.
            daily = rows.groupby(rows['Date'].dt.normalize())[column].sum().sort_index()
            if len(daily) < min_occurrences:
                continue

            dates = list(daily.index)
            intervals = [(b - a).days for a, b in zip(dates, dates[1:])]
            typical = median(intervals)
            cadence = _classify_cadence(typical)
            if cadence is None:
                continue
            slack = max(3.0, 0.25 * typical)
            regular = sum(abs(i - typical) <= slack for i in intervals) / len(intervals)
            if regular < 0.6:
                continue

            amounts = [float(a) for a in daily.tolist()]
            mean_amount = sum(amounts) / len(amounts)
            spread = (sum((a - mean_amount) ** 2 for a in amounts) / len(amounts)) ** 0.5
            fixed = mean_amount > 0 and spread / mean_amount <= 0.15

            last = dates[-1]
            active = last + pd.Timedelta(days=1.5 * typical) >= data_through
            date_strs = [d.strftime('%Y-%m-%d') for d in dates]

            # What it costs *now*: a fixed charge is at its latest price (a
            # subscription that went up is not still costing its old average);
            # a variable one (utilities, say) is best estimated by its mean.
            current = amounts[-1] if fixed else mean_amount

            items.append({
                "payee": payee,
                "direction": direction,
                "cadence": cadence,
                "interval_days": round(float(typical), 1),
                "regularity": round(100 * regular, 0),
                "occurrences": len(dates),
                "first": date_strs[0],
                "last": date_strs[-1],
                "last_amount": round(amounts[-1], 2),
                "avg_amount": round(mean_amount, 2),
                "amount_kind": "fixed" if fixed else "variable",
                "monthly_equivalent": round(current * PER_MONTH[cadence], 2),
                "next_expected": (last + pd.Timedelta(days=round(typical))).strftime('%Y-%m-%d'),
                "status": "active" if active else "stopped",
                "price_changes": _price_changes(date_strs, amounts) if fixed or len(amounts) >= 4 else [],
                "banks": sorted(rows['Bank'].dropna().unique().tolist()),
                "category": rows['matched_category'].mode().iat[0] if not rows['matched_category'].mode().empty else UNCATEGORIZED,
                "sample": str(rows['Description'].iloc[-1]),
            })

    items.sort(key=lambda i: (i["status"] != "active", i["direction"] != "out", -i["monthly_equivalent"]))
    committed = sum(i["monthly_equivalent"] for i in items if i["direction"] == "out" and i["status"] == "active")
    income = sum(i["monthly_equivalent"] for i in items if i["direction"] == "in" and i["status"] == "active")
    return {
        "items": items,
        "committed_monthly_out": round(committed, 2),
        "recurring_monthly_in": round(income, 2),
        "data_through": data_through.strftime('%Y-%m-%d'),
    }


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------

def _spend_in(df, budget, month):
    rows = df[(df['_month'] == month) & (df['Debit'] > 0)]
    if budget['scope'] == 'tag':
        # .astype(bool) + .loc: on an empty frame .apply yields an object
        # Series, which plain rows[mask] would treat as a *column* selection.
        mask = rows['tags'].apply(lambda t: isinstance(t, list) and budget['target'] in t).astype(bool)
        rows = rows.loc[mask]
    else:
        rows = rows[rows['matched_category'] == budget['target']]
    return float(rows['Debit'].sum())


def _typical_remaining(df, budget, month, through_day, lookback=3):
    """
    What this budget usually spends in the part of the month not yet covered,
    as the median over the previous `lookback` months -- or None if the ledger
    doesn't reach back that far.

    This is what makes the projection honest for lumpy spending: rent paid on
    the 3rd is already in `actual`, and in past months nothing more was spent
    after the 3rd, so it isn't projected to happen again. A straight-line
    projection would scale it up by 1/elapsed and call the budget at risk.
    """
    first = pd.Period(month, freq='M') - lookback
    if df.empty or df['Date'].min() > first.start_time:
        return None
    rest = []
    for past in _month_range(str(pd.Period(month, freq='M') - 1), lookback):
        rows = df[(df['_month'] == past) & (df['Date'].dt.day > through_day)]
        rest.append(_spend_in(rows, budget, past))
    return median(rest)


def budget_status(flow, budgets, month=None, history_months=6):
    """
    Actual vs. budget for one month, with pace and optional rollover.

    Spending means debits only. Credits in a category are not netted off,
    because for mixed categories (UPI, say) they're income, not refunds, and
    netting them would make a budget look healthier than it is.

    Rollover accumulates from the budget's start month: every rupee under (or
    over) budget in earlier months is added to (or taken from) this month.

    For a month the statements only partly cover, the projection is what's
    been spent so far plus what is typically spent in the rest of the month
    (see _typical_remaining), so a big payment early in the month doesn't read
    as runaway spending.
    """
    df = _prepared(flow)
    data_through = df['Date'].max() if not df.empty else None
    if month is None:
        month = data_through.strftime('%Y-%m') if data_through is not None else pd.Timestamp.now().strftime('%Y-%m')

    elapsed = elapsed_fraction(month, data_through)
    history_window = _month_range(month, history_months)

    results = []
    for budget in budgets:
        amount = float(budget.get('amount') or 0)
        actual = _spend_in(df, budget, month)

        carry = 0.0
        if budget.get('rollover'):
            start = budget.get('start_month') or month
            if start < month:
                for past in pd.period_range(start, pd.Period(month, freq='M') - 1, freq='M'):
                    carry += amount - _spend_in(df, budget, str(past))
        available = amount + carry

        projected = None
        if elapsed >= 1:
            projected = actual
        elif elapsed > 0:
            through_day = data_through.day
            remaining = _typical_remaining(df, budget, month, through_day)
            # Fall back to straight-line pacing only without enough history.
            projected = actual + remaining if remaining is not None else actual / elapsed
        if elapsed == 0:
            status = 'no_data'
        elif actual > available:
            status = 'over'
        elif elapsed < 1 and projected is not None and projected > available:
            status = 'at_risk'
        else:
            status = 'ok'

        results.append({
            **budget,
            "month": month,
            "actual": round(actual, 2),
            "carry": round(carry, 2),
            "available": round(available, 2),
            "remaining": round(available - actual, 2),
            "pct_used": round(100 * actual / available, 1) if available > 0 else None,
            "pct_elapsed": round(100 * elapsed, 1),
            "projected": round(projected, 2) if projected is not None else None,
            "status": status,
            "history": [{"month": m, "actual": round(_spend_in(df, budget, m), 2)} for m in history_window],
        })

    return {
        "month": month,
        "data_through": data_through.strftime('%Y-%m-%d') if data_through is not None else None,
        "pct_elapsed": round(100 * elapsed, 1),
        "budgets": results,
        "total_budgeted": round(sum(r["available"] for r in results), 2),
        "total_actual": round(sum(r["actual"] for r in results), 2),
    }


def budget_suggestions(flow, month=None, lookback=3):
    """
    A starting amount for each category and tag: the median of its spending
    over the `lookback` complete months before `month`. Median rather than mean
    so one unusual month doesn't set the bar.
    """
    df = _prepared(flow)
    if df.empty:
        return {"categories": [], "tags": []}
    if month is None:
        month = df['Date'].max().strftime('%Y-%m')
    previous = _month_range(str(pd.Period(month, freq='M') - 1), lookback)

    spend = df[(df['Debit'] > 0) & (df['_month'].isin(previous))]

    def summarise(keys_and_rows):
        out = []
        for key, rows in keys_and_rows:
            per_month = rows.groupby('_month')['Debit'].sum().reindex(previous, fill_value=0.0)
            out.append({"target": key, "suggested": round(float(per_month.median()), 0),
                        "average": round(float(per_month.mean()), 2),
                        "months": previous})
        return sorted(out, key=lambda r: -r["average"])

    categories = summarise(spend.groupby('matched_category'))

    if 'tags' in spend and not spend.empty:
        tag_rows = spend.loc[spend['tags'].apply(lambda t: isinstance(t, list) and len(t) > 0).astype(bool)]
    else:
        tag_rows = spend.iloc[0:0]
    exploded = tag_rows.explode('tags') if not tag_rows.empty else tag_rows
    tags = summarise(exploded.groupby('tags')) if not exploded.empty else []

    return {"categories": categories, "tags": tags}
