"""
Annotation recovery.

The payoff for keying annotations off a stable fingerprint: when a key does
move, the work is parked rather than lost, and can be re-attached to a row.
"""
import pandas as pd
from fastapi import APIRouter, HTTPException

import annotations
from api.enrichment import get_enriched_data_df, load_tags, save_tags
from api.models import RelinkRequest

router = APIRouter()

# How far either side of the recorded date to look for the same amount, and
# how many suggestions to offer.
CANDIDATE_WINDOW_DAYS = 3
MAX_CANDIDATES = 5
AMOUNT_EPSILON = 0.01


def _find_candidates(df, context):
    """Rows that could plausibly be the transaction an orphan belonged to."""
    if df.empty or not context.get("date"):
        return []

    target_date = pd.to_datetime(context["date"], errors='coerce')
    if pd.isna(target_date):
        return []

    signed = float(context.get("credit", 0)) - float(context.get("debit", 0))
    window = pd.Timedelta(days=CANDIDATE_WINDOW_DAYS)
    nearby = df[(df['Date'] >= target_date - window) & (df['Date'] <= target_date + window)]

    candidates = []
    for _, row in nearby.iterrows():
        row_signed = float(row.get('Credit') or 0) - float(row.get('Debit') or 0)
        if abs(row_signed - signed) > AMOUNT_EPSILON:
            continue
        candidates.append({
            "anno_key": row['anno_key'],
            "date": row['Date'].strftime('%Y-%m-%d') if pd.notna(row['Date']) else '',
            "bank": row.get('Bank', ''),
            "description": str(row.get('Description', ''))[:120],
            "amount": row_signed,
            "same_bank": str(row.get('Bank', '')) == context.get('bank', ''),
        })

    # Same-bank matches first -- they are overwhelmingly the right answer.
    candidates.sort(key=lambda c: (not c['same_bank'], c['date']))
    return candidates[:MAX_CANDIDATES]


@router.get("/api/annotations/orphans")
async def list_orphaned_annotations():
    """
    Annotations whose transaction is no longer in the ledger, each with
    candidate rows to re-link to.
    """
    # Build the frame FIRST: that pass is what detects keys which no longer
    # match a transaction and moves them into `orphans`. Reading the store
    # before it ran would report nothing on the first call after a re-upload.
    df = get_enriched_data_df()

    orphans = load_tags().get("orphans", {})
    return [
        {
            "orphan_key": key,
            "tags": entry.get("tags", []),
            "remarks": entry.get("remarks", ""),
            "description_override": entry.get("description_override", ""),
            "context": entry.get("context") or {},
            "candidates": _find_candidates(df, entry.get("context") or {}),
        }
        for key, entry in orphans.items()
    ]


@router.post("/api/annotations/relink")
async def relink_annotation(request: RelinkRequest):
    """Re-attach an orphaned annotation to a transaction that is still present."""
    store = load_tags()
    orphan = store.get("orphans", {}).pop(request.orphan_key, None)
    if orphan is None:
        raise HTTPException(status_code=404, detail="Orphaned annotation not found")

    target = annotations.upsert_entry(store, request.target_key)
    target["tags"] = sorted(set(target.get("tags", [])) | set(orphan.get("tags", [])))
    target["excluded_tags"] = sorted(
        set(target.get("excluded_tags", [])) | set(orphan.get("excluded_tags", []))
    )
    # The target's own text wins; the orphan only fills in what is missing.
    if orphan.get("remarks") and not target.get("remarks"):
        target["remarks"] = orphan["remarks"]
    if orphan.get("description_override") and not target.get("description_override"):
        target["description_override"] = orphan["description_override"]
    target["updated_at"] = pd.Timestamp.now().isoformat()

    # Remember the move so any stale reference to the old key still resolves.
    store["key_aliases"][request.orphan_key] = request.target_key
    annotations.log_event(store, "orphan_relink", key=request.target_key,
                          orphan_key=request.orphan_key)

    save_tags(store)
    return {"success": True, "annotation": target}


@router.post("/api/annotations/discard-orphan")
async def discard_orphan(request: RelinkRequest):
    """Drop an orphan the user has decided is not worth re-linking."""
    store = load_tags()
    removed = store.get("orphans", {}).pop(request.orphan_key, None)
    if removed is not None:
        annotations.log_event(store, "orphan_discard", orphan_key=request.orphan_key,
                              discarded=annotations.entry_snapshot(removed))
    save_tags(store)
    return {"success": True, "discarded": removed is not None}
