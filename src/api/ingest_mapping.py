"""
Column-mapping inference for freshly uploaded statements.

Kept apart from the upload route because it is pure header analysis: given a
sheet's columns, guess which standard field each one is.
"""
import pandas as pd

# Ordered because the checks overlap: "credit" would also match a column named
# "credit card debit", so the more specific tests have to come first.
_HINTS = [
    ("Date", lambda c: "date" in c or "value dt" in c),
    ("Description", lambda c: any(x in c for x in
                                  ("description", "desc", "particular", "narration", "details", "remarks"))),
    ("Debit", lambda c: any(x in c for x in ("debit", "dr", "withdrawal")) and "dr / cr" not in c),
    ("Credit", lambda c: any(x in c for x in ("credit", "cr", "deposit")) and "dr / cr" not in c),
    ("Balance", lambda c: "bal" in c),
    ("RefNumber", lambda c: any(x in c for x in ("ref", "chq", "txn", "cheque", "reference"))),
    ("TxnType", lambda c: "type" in c),
    ("Amount", lambda c: "amount" in c),
]

SAMPLE_ROWS = 5
MAX_DISTINCT_SAMPLES = 10


def suggest_mapping(headers):
    """Best-guess standard field for each raw column name."""
    mappings = {}
    for column in headers:
        lowered = str(column).lower()
        for standard, matches in _HINTS:
            if matches(lowered):
                mappings[column] = standard
                break
    return mappings


def new_header_group(headers, bank, filename, frame):
    """Start an aggregate for one distinct header layout."""
    sample_rows = (frame.head(SAMPLE_ROWS)
                   .replace([float('inf'), float('-inf')], 0)
                   .fillna('')
                   .values.tolist())
    return {
        "filenames": [filename],
        "headers_list": list(headers),
        "bank": bank,  # the first bank seen is the representative
        "filled_counts": {h: 0 for h in headers},
        "distinct_samples": {h: set() for h in headers},
        "sample_data": sample_rows,
        "mappings": suggest_mapping(list(headers)),
    }


def accumulate(group, headers, frame):
    """Fold one more file's column statistics into its header group."""
    for idx, column in enumerate(headers):
        values = frame.iloc[:, idx].replace('', pd.NA).dropna()
        group["filled_counts"][column] += int(len(values))

        if len(group["distinct_samples"][column]) < MAX_DISTINCT_SAMPLES:
            group["distinct_samples"][column].update(
                str(v) for v in values.unique()[:MAX_DISTINCT_SAMPLES]
            )


def finalize(grouped):
    """Make the aggregates JSON-serializable and drop always-empty columns."""
    final = {}
    for info in grouped.values():
        info["distinct_samples"] = {
            h: list(s)[:MAX_DISTINCT_SAMPLES] for h, s in info["distinct_samples"].items()
        }
        # A column that is empty in every uploaded file is noise, not a field.
        info["mappings"] = {
            h: standard for h, standard in info["mappings"].items()
            if info["filled_counts"].get(h, 0) > 0
        }
        final["|".join(info["filenames"])] = info
    return final
