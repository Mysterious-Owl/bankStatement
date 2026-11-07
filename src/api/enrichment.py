"""
The enriched transaction frame: the ledger plus everything derived from it.

Every read-side endpoint goes through `get_enriched_data_df`, so rules, tags,
remarks and self-transfer state are resolved exactly once, in one place. The
tag-store accessors live here too because any annotation write has to
invalidate this cache -- keeping them apart just meant remembering to do it.
"""
import os

import pandas as pd

import annotations
import config
from api import helpers
from jsonio import load_json

# Columns get_enriched_data_df adds to the raw ledger.
ENRICHED_COLUMNS = [
    'matched_rule', 'matched_category', 'matched_category_color',
    'extracted_fields', 'tags', 'tag_sources', 'manual_remarks',
    'description_override', 'is_self_transfer', 'transfer_pair_id',
    'transfer_direction', 'transfer_counterparty',
    'is_reviewed', 'is_ignored', 'ignored_reason',
]

UNCATEGORIZED = 'Uncategorized'
UNCATEGORIZED_COLOR = '#94a3b8'

# Rebuilt whenever any input changes: the ledger, rules, or categories
# (annotation writes invalidate it explicitly via save_tags).
_cache = {"data": None, "signature": None}


# --------------------------------------------------------------------------
# Annotation store
# --------------------------------------------------------------------------

def load_tags():
    """The unified annotation store (tags, remarks, overrides, transfers)."""
    return annotations.load_store()


def save_tags(store):
    # Snapshot new annotations from the ledger already in memory, so even an
    # edit made just before a re-import can still be recovered afterwards.
    if _cache["data"] is not None:
        _record_contexts(_cache["data"], store, only_missing=True)
    annotations.save_store(store)
    # The enriched frame embeds tags/remarks, so it must be rebuilt after any
    # annotation change or the UI keeps serving the pre-edit values.
    invalidate_enriched_cache()


def invalidate_enriched_cache():
    """Force the next read to rebuild. Called after any annotation write."""
    _cache["data"] = None


# --------------------------------------------------------------------------
# Enrichment
# --------------------------------------------------------------------------

def _assign_anno_keys(df, store):
    """
    Attach the stable annotation key to every row, then reconcile the store
    against it.

    Keys are computed before enrichment so every downstream lookup (tags,
    remarks, self transfers) uses the parser-resilient fingerprint rather than
    TxnID, which changes whenever extraction logic changes.
    """
    if df.empty:
        df['anno_key'] = []
        return

    anno_keys = [
        annotations.compute_anno_key(
            row.get('Bank'), row.get('Date'), row.get('Debit'),
            row.get('Credit'), row.get('Description')
        )
        for _, row in df.iterrows()
    ]
    df['anno_key'] = anno_keys

    # One-time upgrade of annotations still keyed by legacy TxnIDs, then park
    # anything that no longer matches a row so the re-link UI can surface it
    # instead of it disappearing silently.
    changed = annotations.adopt_legacy_keys(
        store, dict(zip(df['TxnID'].astype(str), anno_keys))
    )
    changed |= annotations.sweep_orphans(store, set(anno_keys))
    changed |= _record_contexts(df, store)
    if changed:
        annotations.save_store(store)


def _context_for(row):
    return annotations.build_context(
        row.get('Bank'), row.get('Date'), row.get('Debit'), row.get('Credit'),
        row.get('Description'), row.get('SourceFile'))


def _record_contexts(df, store, only_missing=False):
    """
    Give every live annotation a snapshot of the row it belongs to.

    The snapshot (date, amounts, bank, description) is what lets the orphan
    dialog suggest where an annotation belongs if a re-parse ever moves its
    key; without it, an orphan has nothing to match on. Only annotated rows
    are touched, and the store is only written when a snapshot changes.
    """
    entries = store.get("transactions", {})
    if only_missing:
        wanted = {k for k, e in entries.items() if not e.get("context")}
    else:
        wanted = set(entries)
    if not wanted or 'anno_key' not in df.columns:
        return False
    annotated = df[df['anno_key'].isin(wanted)].drop_duplicates('anno_key')
    changed = False
    for _, row in annotated.iterrows():
        context = _context_for(row)
        entry = entries[row['anno_key']]
        if entry.get("context") != context:
            entry["context"] = context
            changed = True
    return changed


def _match_rule(description, compiled_rules):
    """Highest-priority rule matching this description, plus its captures."""
    for rule in compiled_rules:
        match = rule['pattern'].search(description)
        if match:
            return rule, helpers.extracted_values(match)
    return None, {}


def _enrich_row(row, idx, compiled_rules, category_map, store, transfers):
    description = str(row.get('Description', ''))
    matched_rule, extracted = _match_rule(description, compiled_rules)

    rule_id = rule_name = category_id = None
    category_name = UNCATEGORIZED
    category_color = UNCATEGORIZED_COLOR

    if matched_rule:
        rule_id = matched_rule['id']
        rule_name = matched_rule['name']
        category_id = matched_rule['category_id']
        category = category_map.get(category_id, {})
        category_name = category.get('name', UNCATEGORIZED)
        category_color = category.get('color', UNCATEGORIZED_COLOR)

    anno_key = row.get('anno_key')
    context = {
        "anno_key": anno_key,
        "txn_id": str(row.get('TxnID', idx)),
        "rule_id": rule_id,
        # NOTE: category_id must be the category's *id*; passing the display
        # name here is what previously stopped category tags from ever
        # resolving outside this function.
        "category_id": category_id,
        "entity_values": extracted,
        "bank": row.get('Bank'),
        "Description": description,
        "rule": rule_name,
        "category": category_name,
        "source_file": row.get('SourceFile'),
    }
    tags, sources = annotations.resolve_tags(context, store)

    entry = annotations.get_entry(store, anno_key) or {}
    transfer = transfers.get(anno_key) or {}
    ignored = store.get('ignored_rows', {}).get(
        annotations.row_key(anno_key, row.get('SourceFile')))

    return (
        rule_name, category_name, category_color, extracted, tags, sources,
        entry.get('remarks', ''),
        entry.get('description_override', ''),
        bool(transfer),
        transfer.get('pair_id', ''),
        transfer.get('direction', ''),
        transfer.get('counterparty', ''),
        bool(entry.get('reviewed')),
        bool(ignored),
        (ignored or {}).get('reason', ''),
    )


def _input_signature(*paths):
    """(path, mtime, size) per input: changes when any file is edited or swapped."""
    signature = []
    for path in paths:
        stat = os.stat(path) if os.path.exists(path) else None
        signature.append((path, stat.st_mtime_ns if stat else 0, stat.st_size if stat else 0))
    return tuple(signature)


def get_enriched_data_df():
    data_path = config.MEGA_STATEMENT_CSV
    rules_path = config.RULES_FILE
    if not os.path.exists(data_path):
        return pd.DataFrame()

    signature = _input_signature(data_path, rules_path, config.CATEGORIES_FILE)
    if _cache["data"] is not None and _cache["signature"] == signature:
        return _cache["data"]

    df = pd.read_csv(data_path)
    store = load_tags()
    category_map = {c['id']: c for c in load_json(config.CATEGORIES_FILE, [])}
    compiled_rules = helpers.compile_rules(load_json(config.RULES_FILE, []), sort_by_priority=True)

    _assign_anno_keys(df, store)

    # Confirmed pairs are what let the dashboard stop counting money moved
    # between the user's own accounts as income and expenditure.
    transfers = annotations.transfer_index(store)

    if df.empty:
        for column in ENRICHED_COLUMNS:
            df[column] = []
    else:
        rows = [
            _enrich_row(row, idx, compiled_rules, category_map, store, transfers)
            for idx, row in df.iterrows()
        ]
        for position, column in enumerate(ENRICHED_COLUMNS):
            df[column] = [row[position] for row in rows]

    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')

    _cache.update({"data": df, "signature": signature})
    return df
