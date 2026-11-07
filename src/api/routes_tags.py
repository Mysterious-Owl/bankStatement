"""Tagging: per-transaction annotations, group rules, and category defaults."""
import uuid
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException

import annotations
from api import helpers
from api.enrichment import get_enriched_data_df, load_tags, save_tags
from api.models import (AnnotateRequest, ApplyTagsRequest, BulkAnnotateRequest,
                        CategoryTagsRequest, DeleteTagRequest,
                        RemarkUpdateRequest, RenameTagRequest,
                        TransactionTagRequest)

router = APIRouter()

# Criteria keys that name a column rather than an extracted entity field.
COLUMN_CRITERIA = ("rule", "category", "bank", "source_file")


def _now():
    return pd.Timestamp.now().isoformat()


def _log_entry_change(store, action, key, before, entry):
    """Log a per-transaction edit as a before/after diff -- skipped if nothing changed."""
    changes = annotations.diff_snapshots(before, annotations.entry_snapshot(entry))
    if changes:
        annotations.log_event(store, action, key=key, changes=changes)


# --- Per-transaction annotations ------------------------------------------

@router.post("/api/transactions/annotate")
async def annotate_transaction(request: AnnotateRequest):
    """
    Single entry point for everything a user attaches to one transaction.

    Any field left unset is untouched, so the grid can save just a remark
    without having to send the tag list back.
    """
    store = load_tags()
    entry = annotations.upsert_entry(store, request.anno_key)
    before = annotations.entry_snapshot(entry)
    annotations.apply_tag_delta(
        entry, add=request.add_tags, remove=request.remove_tags, set_to=request.tags
    )

    if request.remarks is not None:
        entry["remarks"] = request.remarks
    if request.description_override is not None:
        entry["description_override"] = request.description_override
    if request.context:
        entry["context"] = request.context
    entry["updated_at"] = _now()

    _log_entry_change(store, "annotate", request.anno_key, before, entry)
    save_tags(store)
    return {"success": True, "annotation": entry}


@router.post("/api/tags/bulk")
async def bulk_annotate(request: BulkAnnotateRequest):
    """Apply the same tags/remark to many transactions in one write."""
    store = load_tags()
    now = _now()

    for key in request.anno_keys:
        entry = annotations.upsert_entry(store, key)
        annotations.apply_tag_delta(entry, add=request.add_tags, remove=request.remove_tags)
        if request.remarks is not None:
            entry["remarks"] = request.remarks
        entry["updated_at"] = now

    annotations.log_event(store, "bulk_annotate", keys=request.anno_keys,
                          add_tags=request.add_tags, remove_tags=request.remove_tags,
                          remarks=request.remarks)
    save_tags(store)
    return {"success": True, "updated": len(request.anno_keys)}


@router.post("/api/tags/transaction")
async def update_transaction_tags(request: TransactionTagRequest):
    """
    Manual tags for one transaction.

    `mode` controls the semantics: "add" merges (the old, only behaviour),
    "remove" takes tags away (including suppressing inherited ones), and "set"
    replaces the manual list outright.
    """
    store = load_tags()
    entry = annotations.upsert_entry(store, request.txn_id)
    before = annotations.entry_snapshot(entry)

    if request.mode == "set":
        annotations.apply_tag_delta(entry, set_to=request.tags)
    elif request.mode == "remove":
        annotations.apply_tag_delta(entry, remove=request.tags)
    else:
        annotations.apply_tag_delta(entry, add=request.tags)
    entry["updated_at"] = _now()

    _log_entry_change(store, f"tags_{request.mode}", request.txn_id, before, entry)
    save_tags(store)
    return {"success": True, "tags": entry["tags"], "excluded_tags": entry["excluded_tags"]}


@router.post("/api/transactions/remarks")
async def update_remarks(request: RemarkUpdateRequest):
    """Kept for the existing data-grid remark box; keys resolve through aliases."""
    store = load_tags()
    entry = annotations.upsert_entry(store, request.txn_id)
    before = annotations.entry_snapshot(entry)
    entry["remarks"] = request.remarks
    entry["updated_at"] = _now()
    _log_entry_change(store, "remarks", request.txn_id, before, entry)
    save_tags(store)
    return {"success": True, "remarks": entry["remarks"]}


# --- Groups ---------------------------------------------------------------

@router.get("/api/tags/grouped")
async def get_grouped_transactions(columns: Optional[str] = "rule,category"):
    """
    Group transactions by the selected columns and report each group's tags.

    Groups are keyed by an ordered dict of criteria. The delimited
    "rule:X|category:Y" string is still emitted for display and backwards
    compatibility, but the criteria dict is what gets sent back on write --
    round-tripping through the string broke on any value containing a pipe.
    """
    grouping_cols = [c.strip() for c in columns.split(",")]

    df = get_enriched_data_df()
    if df.empty:
        return []

    groups = {}
    for idx, row in df.iterrows():
        criteria = {}
        entity_values = {}

        for col in grouping_cols:
            if col == "rule":
                criteria["rule"] = row.get('matched_rule')
            elif col == "category":
                criteria["category"] = row.get('matched_category')
            elif col == "bank":
                criteria["bank"] = row.get('Bank', '')
            elif col == "source_file":
                criteria["source_file"] = row.get('SourceFile', '')
            else:
                extracted = row.get('extracted_fields', {})
                if isinstance(extracted, dict) and col in extracted:
                    entity_values[col] = extracted[col]
                    criteria[col] = extracted[col]

        group_key = "|".join(f"{k}:{v}" for k, v in criteria.items())

        # Tags were already resolved during enrichment -- recomputing them here
        # is what used to reintroduce the category-id/name mismatch.
        tags = row.get('tags') or []

        group = groups.get(group_key)
        if group is None:
            group = groups[group_key] = {
                'group_key': group_key,
                'criteria': criteria,
                'rule': row.get('matched_rule'),
                'category': row.get('matched_category'),
                'category_color': row.get('matched_category_color'),
                'bank': row.get('Bank'),
                'source_file': row.get('SourceFile'),
                'entity_values': entity_values,
                'count': 0,
                'txn_ids': [],
                'anno_keys': [],
                'tags': [],
                'tag_counts': {},
            }

        group['count'] += 1
        group['txn_ids'].append(str(row.get('TxnID', idx)))
        group['anno_keys'].append(row.get('anno_key'))
        for tag in tags:
            group['tag_counts'][tag] = group['tag_counts'].get(tag, 0) + 1

    # Which tags on a group came from a saved rule (vs. individual manual
    # tagging) -- lets the UI show what is safe to remove group-wide.
    store = load_tags()
    for group in groups.values():
        rule_tags = set()
        for mapping in store.get("rule_tag_mappings", []):
            if mapping.get("criteria") == group['criteria']:
                rule_tags.update(mapping.get("tags", []))
        group['rule_tags'] = sorted(rule_tags)
        group['tags'] = sorted(group['tag_counts'].keys())

    return list(groups.values())


@router.get("/api/tags/details/{group_key:path}")
async def get_group_details(group_key: str):
    """
    All transactions in a group. Tags come from the enriched frame, which
    already resolves them correctly -- this endpoint previously recomputed them
    with the category name in the category-id slot.
    """
    df = get_enriched_data_df()
    if df.empty:
        return []

    criteria = helpers.criteria_from_group_key(group_key)
    filtered = df[helpers.mask_for_criteria(df, criteria)] if criteria else df
    return helpers.records(filtered)


def _reconcile_group_members(store, criteria, removed_tags, added_tags):
    """
    Bring the group's individual transactions in line with the new group rule.

    A tag removed at group level should also disappear from any transaction
    that had it applied by hand, otherwise it looks like it came back from
    nowhere. Conversely, applying a tag to the whole group is an explicit
    instruction, so it clears any earlier per-transaction exclusion of that
    tag -- otherwise some rows silently ignore the new rule.
    """
    df = get_enriched_data_df()
    if df.empty:
        return

    for key in df[helpers.mask_for_criteria(df, criteria)]['anno_key']:
        entry = annotations.get_entry(store, key)
        if entry is None:
            continue
        if removed_tags and entry.get("tags"):
            entry["tags"] = [t for t in entry["tags"] if t not in removed_tags]
        if added_tags and entry.get("excluded_tags"):
            entry["excluded_tags"] = [t for t in entry["excluded_tags"] if t not in added_tags]


@router.post("/api/tags/apply")
async def apply_tags(request: ApplyTagsRequest):
    """
    Set the tag list for a whole group, saved as a criteria rule so future
    transactions matching the same criteria inherit it automatically.
    """
    store = load_tags()
    criteria = helpers.criteria_from_request(request)
    if not criteria:
        raise HTTPException(status_code=400, detail="No criteria supplied for this group")

    mappings = store.setdefault("rule_tag_mappings", [])
    existing = next((m for m in mappings if m.get("criteria") == criteria), None)
    old_tags = set(existing["tags"]) if existing else set()
    removed_tags = old_tags - set(request.tags)
    added_tags = set(request.tags) - old_tags

    if not request.tags:
        # Clearing every tag removes the rule outright rather than leaving an
        # empty mapping behind.
        store["rule_tag_mappings"] = [m for m in mappings if m.get("criteria") != criteria]
    elif existing:
        existing["tags"] = list(request.tags)
        existing["updated_at"] = _now()
    else:
        mappings.append({
            "id": str(uuid.uuid4()),
            "criteria": criteria,
            "tags": list(request.tags),
            "created_at": _now(),
        })

    if removed_tags or added_tags:
        _reconcile_group_members(store, criteria, removed_tags, added_tags)
        annotations.log_event(store, "group_tags", criteria=criteria,
                              before=sorted(old_tags), after=sorted(request.tags))

    save_tags(store)
    return {"success": True, "criteria": criteria, "tags": request.tags}


@router.post("/api/tags/clear")
async def clear_tags(group_key: Optional[str] = None, txn_ids: Optional[List[str]] = None):
    """Clear manual tags on specific transactions, and/or a group's tag rule."""
    store = load_tags()

    for txn_id in (txn_ids or []):
        entry = annotations.get_entry(store, txn_id)
        if entry:
            entry["tags"] = []
            entry["excluded_tags"] = []

    criteria = helpers.criteria_from_group_key(group_key) if group_key else {}
    if criteria:
        rule_id = criteria.get("rule")
        entity_values = {k: v for k, v in criteria.items() if k not in COLUMN_CRITERIA}
        store["rule_tag_mappings"] = [
            m for m in store.get("rule_tag_mappings", [])
            if m.get("criteria") != criteria
            and not (rule_id and m.get("rule_id") == rule_id
                     and m.get("entity_values") == entity_values)
        ]

    annotations.log_event(store, "clear_tags", keys=txn_ids or None, criteria=criteria or None)
    save_tags(store)
    return {"success": True}


# --- Tag vocabulary -------------------------------------------------------

@router.get("/api/tags/vocabulary")
async def get_tag_vocabulary():
    """
    Every tag, with what a rename or delete would actually touch.

    Built from the store rather than from the transactions, because a tag can
    exist with nothing currently carrying it -- a rule that no longer matches
    anything, or a tag left behind on an orphaned annotation. Those are exactly
    the ones worth cleaning up, so they must not be invisible here.
    """
    store = load_tags()
    stats = {
        name: {"name": name, "transactions": 0, "manual": 0,
               "rules": 0, "categories": 0, "orphans": 0}
        for name in annotations.collect_all_tags(store)
    }

    def count(tag, field):
        if tag in stats:
            stats[tag][field] += 1

    for entry in store.get("transactions", {}).values():
        for tag in entry.get("tags", []):
            count(tag, "manual")
    for entry in store.get("orphans", {}).values():
        for tag in entry.get("tags", []):
            count(tag, "orphans")
    for mapping in store.get("rule_tag_mappings", []):
        for tag in mapping.get("tags", []):
            count(tag, "rules")
    for tags in store.get("category_tags", {}).values():
        for tag in tags:
            count(tag, "categories")

    # Effective tags, i.e. after rules and category defaults are resolved and
    # per-transaction exclusions applied -- what the user actually sees.
    df = get_enriched_data_df()
    if not df.empty:
        for tag_list in df['tags']:
            if isinstance(tag_list, list):
                for tag in tag_list:
                    count(tag, "transactions")

    return sorted(stats.values(), key=lambda t: (-t["transactions"], t["name"]))


@router.post("/api/tags/rename")
async def rename_tag(request: RenameTagRequest):
    """
    Rename a tag everywhere it appears -- manual, rule, and category.

    Renaming onto a name that already exists merges the two; the caller is told
    so it can say as much rather than silently halving the tag list.
    """
    old = request.old_name
    new = request.new_name.strip()
    if not new:
        raise HTTPException(status_code=400, detail="New tag name cannot be empty")

    store = load_tags()
    known = annotations.collect_all_tags(store)
    if old not in known:
        raise HTTPException(status_code=404, detail=f"Unknown tag: {old}")
    merged = new != old and new in known

    annotations.map_tags(store, lambda tags: sorted({new if t == old else t for t in tags}))
    annotations.log_event(store, "tag_rename", tag=old, new_name=new, merged=merged)
    save_tags(store)
    return {"success": True, "old_name": old, "new_name": new, "merged": merged}


@router.post("/api/tags/delete")
async def delete_tag(request: DeleteTagRequest):
    """Remove a tag from the entire system."""
    name = request.name
    store = load_tags()
    annotations.map_tags(store, lambda tags: [t for t in tags if t != name])

    # A criteria rule whose only tag was the deleted one has nothing left to do.
    store["rule_tag_mappings"] = [
        m for m in store.get("rule_tag_mappings", []) if m.get("tags")
    ]

    annotations.log_event(store, "tag_delete", tag=name)
    save_tags(store)
    return {"success": True, "deleted": name}


@router.get("/api/tags/autocomplete")
async def get_tag_autocomplete():
    """Every tag known to the system, for autocomplete."""
    return annotations.collect_all_tags(load_tags())


# --- Category defaults ----------------------------------------------------

@router.get("/api/tags/category")
async def get_category_tags():
    return load_tags().get("category_tags", {})


@router.post("/api/tags/category")
async def set_category_tags(request: CategoryTagsRequest):
    """Set default tags for a category (inherited by every matching txn)."""
    store = load_tags()
    category_tags = store.setdefault("category_tags", {})
    before = list(category_tags.get(request.category_id, []))
    category_tags[request.category_id] = request.tags
    if before != list(request.tags):
        annotations.log_event(store, "category_tags", category_id=request.category_id,
                              before=before, after=list(request.tags))
    save_tags(store)
    return {"success": True}
