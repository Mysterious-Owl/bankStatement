"""Detecting and confirming transfers between the user's own accounts."""
import hashlib
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter

import annotations
from api import helpers
from api.enrichment import get_enriched_data_df, load_tags, save_tags
from api.models import SelfTransferSaveRequest, UnmarkTransferRequest

router = APIRouter()

DEFAULT_SEARCH_WINDOW_DAYS = 90


def _make_pair_id(debit_key, credit_key):
    return hashlib.md5(f"{debit_key}_{credit_key}".encode()).hexdigest()[:16]


def _bucket_by_amount(credits):
    """
    Index credits by rounded amount.

    Matching is then a dictionary lookup per debit rather than a full scan of
    every credit -- the old nested loop was O(debits x credits) and got slow as
    the ledger grew.
    """
    buckets = {}
    for _, credit in credits.iterrows():
        buckets.setdefault(round(float(credit['Credit']), 2), []).append(credit)
    return buckets


def _leg(row, column):
    return {
        "Date": row['Date'].strftime('%Y-%m-%d'),
        "Bank": row['Bank'],
        "Description": str(row.get('Description', '')),
        column: float(row[column]),
    }


@router.get("/api/self-transfers/search")
async def search_self_transfers(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    max_day_gap: int = 1,
    amount_tolerance: float = 0.0,
):
    """
    Candidate transfer pairs: same amount out of one bank and into another
    within a short window. Already-confirmed pairs are excluded so the user
    isn't asked to re-confirm the same transfers on every search.
    """
    df = get_enriched_data_df()
    if df.empty:
        return []

    df = helpers.filter_rows(df, start_date, end_date)

    claimed = annotations.claimed_transfer_keys(load_tags())
    unclaimed = df[~df['anno_key'].isin(claimed)]
    credit_buckets = _bucket_by_amount(unclaimed[unclaimed['Credit'] > 0])

    window = pd.Timedelta(days=max_day_gap)
    pairs = []
    for _, debit in unclaimed[unclaimed['Debit'] > 0].iterrows():
        amount = round(float(debit['Debit']), 2)

        if amount_tolerance > 0:
            # Lets a transfer still match when the receiving bank credits
            # slightly less (or more) than was debited, e.g. after a fee.
            candidate_amounts = [a for a in credit_buckets if abs(a - amount) <= amount_tolerance]
        else:
            candidate_amounts = [amount]

        for candidate in candidate_amounts:
            for credit in credit_buckets.get(candidate, []):
                if credit['Bank'] == debit['Bank']:
                    continue
                if not (debit['Date'] <= credit['Date'] <= debit['Date'] + window):
                    continue

                pairs.append({
                    "pair_id": _make_pair_id(debit['anno_key'], credit['anno_key']),
                    "debit_key": debit['anno_key'],
                    "credit_key": credit['anno_key'],
                    "amount": amount,
                    "credited_amount": round(float(credit['Credit']), 2),
                    "debit_txn": _leg(debit, 'Debit'),
                    "credit_txn": _leg(credit, 'Credit'),
                })

    pairs.sort(key=lambda p: p['debit_txn']['Date'], reverse=True)
    return pairs


@router.post("/api/self-transfers/save")
async def save_self_transfers(pairs: List[SelfTransferSaveRequest]):
    """
    Confirm pairs as self transfers. Only the two stable annotation keys plus a
    little context are stored -- the old format duplicated both full
    transaction payloads, which bloated the file and broke as soon as TxnIDs
    changed.
    """
    store = load_tags()
    existing_ids = {p.get('pair_id') for p in store.get("self_transfers", [])}
    claimed = annotations.claimed_transfer_keys(store)

    added = 0
    skipped = 0
    for pair in pairs:
        if pair.pair_id in existing_ids:
            continue
        # A transaction can only be one leg of one transfer.
        if pair.debit_key in claimed or pair.credit_key in claimed:
            skipped += 1
            continue

        store["self_transfers"].append({
            "pair_id": pair.pair_id,
            "debit_key": pair.debit_key,
            "credit_key": pair.credit_key,
            "amount": pair.amount,
            "debit_date": pair.debit_date,
            "credit_date": pair.credit_date,
            "from_bank": pair.from_bank,
            "to_bank": pair.to_bank,
            "confirmed_at": pd.Timestamp.now().isoformat(),
        })
        existing_ids.add(pair.pair_id)
        claimed.update({pair.debit_key, pair.credit_key})
        added += 1
        annotations.log_event(store, "transfer_confirm", pair_id=pair.pair_id,
                              keys=[pair.debit_key, pair.credit_key], amount=pair.amount)

    save_tags(store)
    return {
        "status": "success",
        "added": added,
        "skipped_already_paired": skipped,
        "total": len(store["self_transfers"]),
    }


@router.post("/api/self-transfers/unmark")
async def unmark_self_transfer(request: UnmarkTransferRequest):
    """Undo a confirmation -- both legs go back to counting as normal flow."""
    store = load_tags()
    before = len(store.get("self_transfers", []))
    store["self_transfers"] = [
        p for p in store.get("self_transfers", []) if p.get("pair_id") != request.pair_id
    ]
    removed = before - len(store["self_transfers"])
    if removed:
        annotations.log_event(store, "transfer_unmark", pair_id=request.pair_id)
    save_tags(store)
    return {"success": True, "removed": removed}


@router.get("/api/self-transfers/confirmed")
async def list_confirmed_self_transfers():
    return sorted(
        load_tags().get("self_transfers", []),
        key=lambda p: p.get("debit_date", ""),
        reverse=True,
    )


@router.get("/api/self-transfers/meta")
async def get_self_transfer_meta():
    """
    Default search window: the last 3 months of the ledger, or resume from the
    most recent confirmation so a repeat search starts where the user left off.
    """
    store = load_tags()
    dates = [p.get("debit_date") for p in store.get("self_transfers", []) if p.get("debit_date")]

    # Anchor on where the statements end, not on today: statements arrive
    # late, and a window ending today can easily contain no transactions.
    df = get_enriched_data_df()
    ledger_end = df['Date'].max() if not df.empty and df['Date'].notna().any() else pd.Timestamp.now()

    if dates:
        start_date = pd.to_datetime(max(dates)).strftime('%Y-%m-%d')
    else:
        start_date = (ledger_end - pd.Timedelta(days=DEFAULT_SEARCH_WINDOW_DAYS)).strftime('%Y-%m-%d')

    return {
        "start_date": start_date,
        "end_date": ledger_end.strftime('%Y-%m-%d'),
        "confirmed_count": len(store.get("self_transfers", [])),
    }
