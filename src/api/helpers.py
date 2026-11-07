"""Small pieces shared by more than one router."""
import re

import pandas as pd

import frames

# Criteria keys that address a column directly; anything else is looked up in
# the rule's extracted entity fields.
_CRITERIA_COLUMNS = {
    "rule": "matched_rule",
    "category": "matched_category",
    "bank": "Bank",
    "source_file": "SourceFile",
}


def compile_rules(rules, sort_by_priority=False):
    """
    Compile rule patterns, skipping any that are not valid regexes.

    A bad pattern is a user typo in one rule; it must not take down the whole
    endpoint, so it is dropped rather than raised.
    """
    compiled = []
    for rule in rules:
        try:
            compiled.append({
                'id': rule['id'],
                'name': rule['name'],
                'pattern': re.compile(rule['pattern'], re.IGNORECASE),
                'priority': rule.get('priority', 0),
                'category_id': rule.get('category_id'),
            })
        except (re.error, KeyError):
            continue
    if sort_by_priority:
        compiled.sort(key=lambda r: r['priority'], reverse=True)
    return compiled


def extracted_values(match):
    """Named groups if the pattern has any, else positional ones."""
    if match.groupdict():
        return match.groupdict()
    return {f'group_{i + 1}': g for i, g in enumerate(match.groups())}


def txn_summary(idx, row, **extra):
    """The transaction shape the rule-inspection endpoints return."""
    summary = {
        'index': int(idx),
        'date': row.get('Date'),
        'description': str(row.get('Description', '')),
        'debit': row.get('Debit'),
        'credit': row.get('Credit'),
        'bank': row.get('Bank'),
    }
    summary.update(extra)
    return summary


def criteria_from_group_key(group_key):
    """
    Parse the delimited "rule:X|category:Y" group key.

    Only for older callers -- the string cannot represent a value containing a
    pipe, which is why the structured criteria dict is authoritative now.
    """
    criteria = {}
    for part in (group_key or "").split("|"):
        if ":" in part:
            key, val = part.split(":", 1)
            if key == "source":
                key = "source_file"
            criteria[key] = val
    return criteria


def criteria_from_request(request):
    """Prefer the structured criteria dict; fall back to the delimited key."""
    if getattr(request, "criteria", None):
        return dict(request.criteria)
    return criteria_from_group_key(getattr(request, "group_key", ""))


def mask_for_criteria(df, criteria):
    """Row mask for every transaction a criteria dict applies to."""
    mask = pd.Series(True, index=df.index)
    for key, val in criteria.items():
        column = _CRITERIA_COLUMNS.get(key)
        if column:
            mask &= (df[column].astype(str) == str(val))
        else:
            mask &= df['extracted_fields'].apply(
                lambda x, k=key, v=val: str(x.get(k, '')) == str(v) if isinstance(x, dict) else False
            )
    return mask


def records(df):
    """JSON-safe records with a `txn_id` alias the frontend expects."""
    import json
    rows = json.loads(df.to_json(orient='records', date_format='iso'))
    for row in rows:
        row['txn_id'] = str(row.get('TxnID', ''))
    return rows


def filter_rows(df, start_date=None, end_date=None, banks=None):
    """
    Rows inside an inclusive date range and (optionally) a comma-separated
    bank list -- the filter every list/summary endpoint accepts.
    """
    if start_date:
        df = df[df['Date'] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df['Date'] <= pd.to_datetime(end_date)]
    if banks:
        df = df[df['Bank'].isin(banks.split(','))]
    return df


# Which rows count -- defined once in frames, re-exported for the routers.
bool_column = frames.bool_column
live_rows = frames.live_rows
flow_rows = frames.flow_rows
