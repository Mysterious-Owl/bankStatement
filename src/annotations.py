"""
Unified store for everything a user manually attaches to a transaction:
tags, remarks, description overrides, and self-transfer pairings.

Why this module exists
----------------------
Manual annotations used to be keyed by `TxnID` -- an MD5 over
Date + Description + Debit + Credit + Balance + RefNumber. Any change to date
parsing, description cleanup, or column mapping re-hashed every row, which
silently orphaned every tag and remark the user had ever written. Nothing
detected it and nothing could recover it, because the old files stored only
`txn_id -> tags` with no record of *what* had been annotated.

Two changes fix that:

1. Annotations are keyed by `anno_key`, a deliberately NARROW fingerprint --
   bank + date + signed amount + a normalized description prefix. It leaves out
   the volatile parts of a row (balance, reference number, description
   whitespace/casing/punctuation), so ordinary parser changes no longer move it.

2. Every annotation carries a `context` snapshot of the row it was made
   against. Even if a key does move, the annotation can always be re-linked to
   a candidate row later instead of vanishing.

Note on collisions: two rows in the same bank, on the same day, for the same
signed amount, whose descriptions agree for the first 40 normalized characters
will share an `anno_key`. The pipeline already collapses exact duplicates
before this point, so in practice such rows represent the same transaction and
sharing an annotation is the desired behaviour.
"""

import hashlib
import re
from datetime import datetime

import config
from jsonio import load_json, save_json

SCHEMA_VERSION = 2

# How much of the (normalized) description feeds the fingerprint. Long enough to
# separate distinct transactions, short enough that trailing junk -- reference
# numbers, padding, bank-appended suffixes -- doesn't shift the key.
DESC_KEY_CHARS = 40


def _empty_store():
    return {
        "version": SCHEMA_VERSION,
        # anno_key -> {tags, excluded_tags, remarks, description_override, context, updated_at}
        "transactions": {},
        # old key (legacy TxnID or superseded anno_key) -> current anno_key
        "key_aliases": {},
        # criteria-based auto-tagging: [{id, criteria: {...}, tags: [...]}]
        "rule_tag_mappings": [],
        # category_id -> [tags]
        "category_tags": {},
        # [{pair_id, debit_key, credit_key, amount, ...}]
        "self_transfers": [],
        # annotations whose key no longer exists in the ledger, kept verbatim
        # so the UI can offer a re-link instead of losing the user's work
        "orphans": {},
        # append-only audit trail of every change: [{ts, action, ...detail}]
        "events": [],
        # near-duplicate groups the user has looked at and judged distinct
        "dismissed_duplicates": [],
        # rows left out of every total, keyed by row_key() -- NOT by anno_key,
        # because the two copies of a duplicate usually share an anno_key and
        # ignoring one must not ignore the other: {row_key: {reason, at}}
        "ignored_rows": {},
    }


def normalize_description(description):
    """
    Strip a description down to the part that's stable across parser changes:
    lowercase, alphanumerics only. Collapses the whitespace/punctuation/casing
    differences that used to change TxnID (and therefore orphan annotations).
    """
    return re.sub(r'[^a-z0-9]', '', str(description or '').lower())


def normalize_date(value):
    """Reduce any date representation to a bare ISO date (no time component)."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    # Handles '2024-05-01', '2024-05-01 00:00:00', '2024-05-01T00:00:00Z'
    return re.split(r'[ T]', text)[0]


def compute_anno_key(bank, date, debit, credit, description):
    """
    The stable annotation key. Built from only the fields that identify a
    transaction to a human: which account, which day, how much moved and in
    which direction, and roughly what it said.
    """
    def _num(value):
        try:
            if value is None or value == "":
                return 0.0
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    signed_amount = round(_num(credit) - _num(debit), 2)
    fingerprint = "|".join([
        str(bank or "").strip().upper(),
        normalize_date(date),
        f"{signed_amount:.2f}",
        normalize_description(description)[:DESC_KEY_CHARS],
    ])
    return hashlib.md5(fingerprint.encode('utf-8')).hexdigest()[:16]


def build_context(bank, date, debit, credit, description, source_file=None):
    """
    The snapshot stored beside every annotation. This is what makes an orphaned
    annotation recoverable -- without it there's no way to tell what a stale key
    was ever attached to.
    """
    return {
        "bank": str(bank or ""),
        "date": normalize_date(date),
        "debit": float(debit or 0) if str(debit or "").strip() not in ("", "nan") else 0.0,
        "credit": float(credit or 0) if str(credit or "").strip() not in ("", "nan") else 0.0,
        "description": str(description or "")[:200],
        "source_file": str(source_file or ""),
    }


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def load_store():
    """Load the annotation store, migrating the legacy files on first run."""
    store = load_json(config.ANNOTATIONS_FILE, None)
    if store is None:
        store = migrate_legacy()
        save_store(store)
        return store

    # Fill in any keys added by a later schema version
    base = _empty_store()
    base.update(store)
    base["version"] = SCHEMA_VERSION
    return base


def save_store(store):
    save_json(config.ANNOTATIONS_FILE, store)


def migrate_legacy():
    """
    Fold the old tags.json / remarks.json / self_transfers.json into one store.

    Legacy entries are keyed by TxnID, which we cannot convert to an anno_key
    without the ledger row it came from -- so they're recorded as aliases and
    resolved lazily on the first enrichment pass (see `adopt_legacy_keys`). The
    original files are left on disk untouched as a fallback.
    """
    store = _empty_store()

    legacy_tags = load_json(config.TAGS_FILE, {}) or {}
    for txn_id, info in (legacy_tags.get("transaction_tags") or {}).items():
        store["transactions"][txn_id] = {
            "tags": list(info.get("tags", [])),
            "excluded_tags": [],
            "remarks": "",
            "description_override": "",
            "context": {},
            "updated_at": info.get("applied_at", ""),
            "legacy_txn_id": txn_id,
        }
    store["rule_tag_mappings"] = list(legacy_tags.get("rule_tag_mappings") or [])
    store["category_tags"] = dict(legacy_tags.get("category_tags") or {})

    legacy_remarks = load_json(config.REMARKS_FILE, {}) or {}
    for txn_id, remark in legacy_remarks.items():
        entry = store["transactions"].setdefault(txn_id, {
            "tags": [],
            "excluded_tags": [],
            "remarks": "",
            "description_override": "",
            "context": {},
            "updated_at": "",
            "legacy_txn_id": txn_id,
        })
        entry["remarks"] = remark

    for pair in load_json(config.SELF_TRANSFERS_FILE, []) or []:
        debit_txn = pair.get("debit_txn", {}) or {}
        credit_txn = pair.get("credit_txn", {}) or {}
        store["self_transfers"].append({
            "pair_id": pair.get("pair_id"),
            # Legacy pairs reference TxnIDs; adopt_legacy_keys() upgrades them.
            "debit_key": str(debit_txn.get("TxnID", "")),
            "credit_key": str(credit_txn.get("TxnID", "")),
            "amount": debit_txn.get("Debit", 0),
            "debit_date": normalize_date(debit_txn.get("Date")),
            "credit_date": normalize_date(credit_txn.get("Date")),
            "from_bank": debit_txn.get("Bank", ""),
            "to_bank": credit_txn.get("Bank", ""),
            "confirmed_at": "",
            "legacy": True,
        })

    return store


def adopt_legacy_keys(store, txnid_to_annokey):
    """
    Upgrade annotations still keyed by a legacy TxnID onto their anno_key, using
    a TxnID -> anno_key map built from the current ledger.

    Anything that can't be matched is moved to `orphans` (with whatever context
    we have) rather than silently dropped, so the re-link UI can surface it.
    Returns True if the store changed and needs saving.
    """
    changed = False

    for legacy_id, anno_key in txnid_to_annokey.items():
        if legacy_id == anno_key:
            continue
        entry = store["transactions"].pop(legacy_id, None)
        if entry is None:
            continue

        target = store["transactions"].get(anno_key)
        if target:
            # Merge rather than clobber -- both may hold user work.
            target["tags"] = sorted(set(target.get("tags", [])) | set(entry.get("tags", [])))
            target["excluded_tags"] = sorted(
                set(target.get("excluded_tags", [])) | set(entry.get("excluded_tags", []))
            )
            if entry.get("remarks") and not target.get("remarks"):
                target["remarks"] = entry["remarks"]
            if entry.get("description_override") and not target.get("description_override"):
                target["description_override"] = entry["description_override"]
        else:
            store["transactions"][anno_key] = entry

        store["key_aliases"][legacy_id] = anno_key
        changed = True

    for pair in store["self_transfers"]:
        for side in ("debit_key", "credit_key"):
            mapped = txnid_to_annokey.get(pair.get(side))
            if mapped and mapped != pair.get(side):
                pair[side] = mapped
                changed = True

    return changed


def sweep_orphans(store, live_keys):
    """
    Move annotations whose key isn't in the ledger any more into `orphans`, and
    pull any orphan whose key has reappeared back into active use.

    Keeping them addressable (instead of deleting) is the whole point: an
    orphan still carries its context snapshot, so it can be re-linked.
    Returns True if the store changed.
    """
    changed = False

    for key in list(store["transactions"].keys()):
        if key in live_keys:
            continue
        entry = store["transactions"][key]
        if not _has_content(entry):
            del store["transactions"][key]
            changed = True
            continue
        store["orphans"][key] = entry
        del store["transactions"][key]
        changed = True

    for key in list(store.get("orphans", {}).keys()):
        if key in live_keys:
            store["transactions"][key] = store["orphans"].pop(key)
            changed = True

    return changed


def _has_content(entry):
    return bool(
        entry.get("tags")
        or entry.get("excluded_tags")
        or (entry.get("remarks") or "").strip()
        or (entry.get("description_override") or "").strip()
        or entry.get("reviewed")
    )


def resolve_key(store, key):
    """Follow the alias chain so old keys keep working after a re-link."""
    seen = set()
    current = str(key)
    while current in store.get("key_aliases", {}) and current not in seen:
        seen.add(current)
        current = store["key_aliases"][current]
    return current


def get_entry(store, key):
    return store["transactions"].get(resolve_key(store, key))


def upsert_entry(store, key, **fields):
    """Create or update one transaction's annotation, returning the entry."""
    resolved = resolve_key(store, key)
    entry = store["transactions"].setdefault(resolved, {
        "tags": [],
        "excluded_tags": [],
        "remarks": "",
        "description_override": "",
        "context": {},
        "updated_at": "",
        # audit state: has a human checked this transaction
        "reviewed": False,
        "reviewed_at": "",
    })
    entry.update(fields)
    return entry


# --------------------------------------------------------------------------
# Tag resolution
# --------------------------------------------------------------------------

def resolve_tags(ctx, store):
    """
    Work out the effective tags for one transaction.

    Tags come from three layers -- manual, criteria rules, and the transaction's
    category -- which are unioned and then filtered by the transaction's own
    `excluded_tags`. That exclusion list is what lets a user drop a single
    inherited tag from one transaction without deleting the rule that produced
    it, which the old union-only resolution had no way to express.

    Returns (tags, sources) where sources maps each tag to where it came from.
    """
    sources = {}

    entry = get_entry(store, ctx.get("anno_key", "")) or {}
    for tag in entry.get("tags", []):
        sources[tag] = "manual"

    for mapping in store.get("rule_tag_mappings", []):
        if _criteria_match(mapping.get("criteria", {}), ctx, mapping):
            for tag in mapping.get("tags", []):
                sources.setdefault(tag, "rule")

    category_id = ctx.get("category_id")
    if category_id and category_id in store.get("category_tags", {}):
        for tag in store["category_tags"][category_id]:
            sources.setdefault(tag, "category")

    for tag in entry.get("excluded_tags", []):
        sources.pop(tag, None)

    return sorted(sources.keys()), sources


def _criteria_match(criteria, ctx, mapping):
    if not criteria:
        # Legacy shape: {rule_id, entity_values}
        rule_id = mapping.get("rule_id")
        if not rule_id or ctx.get("rule_id") != rule_id:
            return False
        entities = ctx.get("entity_values") or {}
        return all(str(entities.get(k, "")) == str(v) for k, v in (mapping.get("entity_values") or {}).items())

    entities = ctx.get("entity_values") or {}
    for key, expected in criteria.items():
        expected_str = str(expected)
        if str(ctx.get(key, "")) == expected_str:
            continue
        if str(entities.get(key, "")) == expected_str:
            continue
        return False
    return True


def collect_all_tags(store):
    """Every tag known to the system, for autocomplete."""
    tags = set()
    for entry in store["transactions"].values():
        tags.update(entry.get("tags", []))
    for entry in store.get("orphans", {}).values():
        tags.update(entry.get("tags", []))
    for mapping in store.get("rule_tag_mappings", []):
        tags.update(mapping.get("tags", []))
    for cat_tags in store.get("category_tags", {}).values():
        tags.update(cat_tags)
    return sorted(tags)


# --------------------------------------------------------------------------
# Mutation helpers
# --------------------------------------------------------------------------

def apply_tag_delta(entry, add=None, remove=None, set_to=None):
    """
    Apply a tag change to one annotation entry, in place.

    Removing a tag records it in `excluded_tags` as well as dropping it from the
    manual list: that is what suppresses a tag inherited from a rule or category
    for this one transaction. Adding a tag is the inverse and clears any earlier
    exclusion, so an explicit add always wins over a stale opt-out.
    """
    manual = set(entry.get("tags", []))
    excluded = set(entry.get("excluded_tags", []))

    if set_to is not None:
        manual = set(set_to)
        excluded -= manual
    if add:
        manual |= set(add)
        excluded -= set(add)
    if remove:
        for tag in remove:
            manual.discard(tag)
            excluded.add(tag)

    entry["tags"] = sorted(manual)
    entry["excluded_tags"] = sorted(excluded)
    return entry


def map_tags(store, transform):
    """
    Rewrite every tag list in the store through `transform`, in place.

    Rename and delete both need to reach the same five places (manual tags,
    per-transaction exclusions, orphans, criteria rules, category defaults);
    walking them once here keeps the two from drifting apart.
    """
    for bucket in ("transactions", "orphans"):
        for entry in store.get(bucket, {}).values():
            entry["tags"] = transform(entry.get("tags", []))
            entry["excluded_tags"] = transform(entry.get("excluded_tags", []))

    for mapping in store.get("rule_tag_mappings", []):
        mapping["tags"] = transform(mapping.get("tags", []))

    for category_id, tags in list(store.get("category_tags", {}).items()):
        store["category_tags"][category_id] = transform(tags)


def claimed_transfer_keys(store):
    """Annotation keys already committed to a confirmed self-transfer pair."""
    keys = set()
    for pair in store.get("self_transfers", []):
        keys.add(pair.get("debit_key"))
        keys.add(pair.get("credit_key"))
    keys.discard(None)
    return keys


def transfer_index(store):
    """anno_key -> {pair_id, direction, counterparty} for confirmed transfers."""
    index = {}
    for pair in store.get("self_transfers", []):
        index[pair.get("debit_key")] = {
            "pair_id": pair.get("pair_id"),
            "direction": "out",
            "counterparty": pair.get("to_bank", ""),
        }
        index[pair.get("credit_key")] = {
            "pair_id": pair.get("pair_id"),
            "direction": "in",
            "counterparty": pair.get("from_bank", ""),
        }
    return index


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------

def row_key(anno_key, source_file):
    """
    Identity of one physical row: the transaction plus the file it came from.

    Two copies of the same transaction from two overlapping statements share an
    anno_key but never a source file, so this tells them apart -- and unlike
    TxnID it survives parser changes, since filenames don't change.
    """
    return f"{anno_key}|{source_file}"


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def log_event(store, action, **detail):
    """
    Append one entry to the store's change log.

    The log is append-only on purpose: `updated_at` on an entry only says that
    something changed, not what it was before, which is exactly what an audit
    needs when a categorisation is questioned months later.
    """
    event = {"ts": now_iso(), "action": action}
    event.update({k: v for k, v in detail.items() if v is not None})
    store.setdefault("events", []).append(event)
    return event


def entry_snapshot(entry):
    """The user-visible parts of an entry, for before/after diffs in the log."""
    return {
        "tags": list(entry.get("tags", [])),
        "excluded_tags": list(entry.get("excluded_tags", [])),
        "remarks": entry.get("remarks", ""),
        "description_override": entry.get("description_override", ""),
        "reviewed": bool(entry.get("reviewed")),
    }


def diff_snapshots(before, after):
    """{field: [old, new]} for every field that actually changed."""
    return {k: [before[k], after[k]] for k in after if before.get(k) != after[k]}
