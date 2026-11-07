"""Audit: balance continuity, near-duplicates, review state, and the change log."""
from typing import Optional

from fastapi import APIRouter, HTTPException

import annotations
import audit
from api.enrichment import get_enriched_data_df, load_tags, save_tags
from api.models import DismissDuplicateRequest, IgnoreRowsRequest, ReviewRequest

router = APIRouter()

MAX_EVENTS = 500


@router.get("/api/audit/balance")
async def get_balance_continuity():
    """Per-bank running-balance walk, with every point the arithmetic breaks."""
    return audit.balance_continuity(get_enriched_data_df())


@router.get("/api/audit/duplicates")
async def get_near_duplicates():
    df = get_enriched_data_df()
    return audit.near_duplicates(df, load_tags().get("dismissed_duplicates", []))


@router.post("/api/audit/duplicates/dismiss")
async def dismiss_duplicate(request: DismissDuplicateRequest):
    """The user has checked a group and it is genuinely separate transactions."""
    store = load_tags()
    dismissed = store.setdefault("dismissed_duplicates", [])
    if request.group_id not in dismissed:
        dismissed.append(request.group_id)
        annotations.log_event(store, "dismiss_duplicate", group_id=request.group_id)
    save_tags(store)
    return {"success": True}


@router.post("/api/audit/ignore")
async def set_ignored(request: IgnoreRowsRequest):
    """
    Leave specific rows out of every total (or put them back).

    Keyed by row, not transaction: the two copies of a duplicate share an
    anno_key, and ignoring one copy must leave the other counted.
    """
    store = load_tags()
    ignored = store.setdefault("ignored_rows", {})
    for key in request.row_keys:
        if request.ignored:
            ignored[key] = {"reason": request.reason, "at": annotations.now_iso()}
        else:
            ignored.pop(key, None)
    annotations.log_event(store, "ignore" if request.ignored else "unignore",
                          rows=request.row_keys, reason=request.reason if request.ignored else None)
    save_tags(store)
    return {"success": True, "count": len(request.row_keys)}


@router.post("/api/audit/review")
async def set_reviewed(request: ReviewRequest):
    store = load_tags()
    stamp = annotations.now_iso() if request.reviewed else ""
    for key in request.anno_keys:
        entry = annotations.upsert_entry(store, key)
        entry["reviewed"] = request.reviewed
        entry["reviewed_at"] = stamp
    annotations.log_event(store, "review" if request.reviewed else "unreview",
                          keys=request.anno_keys)
    save_tags(store)
    return {"success": True, "count": len(request.anno_keys)}


@router.get("/api/audit/review-progress")
async def get_review_progress():
    return audit.review_progress(get_enriched_data_df())


@router.get("/api/audit/events")
async def get_events(key: Optional[str] = None, action: Optional[str] = None, limit: int = 100):
    """
    The change log, newest first. `key` narrows it to one transaction's history
    (matching both single-key and multi-key events).
    """
    if limit < 1 or limit > MAX_EVENTS:
        raise HTTPException(status_code=400, detail=f"limit must be 1..{MAX_EVENTS}")

    events = load_tags().get("events", [])
    selected = []
    for event in reversed(events):
        if action and event.get("action") != action:
            continue
        if key and event.get("key") != key and key not in (event.get("keys") or []) \
                and not any(str(r).startswith(f"{key}|") for r in (event.get("rows") or [])):
            continue
        selected.append(event)
        if len(selected) >= limit:
            break
    return {"total": len(events), "events": selected}
