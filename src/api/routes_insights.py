"""Spending insights and budgets."""
import uuid
from typing import Optional

import pandas as pd
from fastapi import APIRouter, HTTPException

import analytics
import annotations
import config
from api import helpers
from api.enrichment import get_enriched_data_df, load_tags, save_tags
from jsonio import load_json, save_json
from api.models import Budget

router = APIRouter()

BUDGET_SCOPES = ("category", "tag")


def _flow(banks: Optional[str] = None):
    df = get_enriched_data_df()
    if df.empty:
        return df
    if banks:
        df = df[df['Bank'].isin(banks.split(','))]
    return helpers.flow_rows(df)


def _category_colors():
    return {c['name']: c.get('color', '#8e8e93') for c in load_json(config.CATEGORIES_FILE, [])}


def _valid_month(month):
    if month is None:
        return None
    try:
        return str(pd.Period(month, freq='M'))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")


# --- Insights -------------------------------------------------------------

@router.get("/api/insights/trends")
async def get_trends(months: int = 12, banks: Optional[str] = None):
    if not 2 <= months <= 60:
        raise HTTPException(status_code=400, detail="months must be 2..60")
    return analytics.monthly_trends(_flow(banks), months=months, category_colors=_category_colors())


@router.get("/api/insights/recurring")
async def get_recurring(banks: Optional[str] = None):
    return analytics.detect_recurring(_flow(banks))


@router.get("/api/insights/coverage")
async def get_coverage(start_date: Optional[str] = None, end_date: Optional[str] = None,
                       banks: Optional[str] = None):
    return analytics.uncategorized_coverage(_flow(banks), start_date, end_date)


# --- Budgets --------------------------------------------------------------

@router.get("/api/budgets")
async def list_budgets():
    return load_json(config.BUDGETS_FILE, [])


@router.post("/api/budgets")
async def save_budget(budget: Budget):
    if budget.scope not in BUDGET_SCOPES:
        raise HTTPException(status_code=400, detail=f"scope must be one of {BUDGET_SCOPES}")
    if budget.amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")
    if not budget.target.strip():
        raise HTTPException(status_code=400, detail="target is required")

    budgets = load_json(config.BUDGETS_FILE, [])
    record = budget.dict()
    record["start_month"] = _valid_month(record.get("start_month")) or pd.Timestamp.now().strftime('%Y-%m')

    existing = next((i for i, b in enumerate(budgets) if b.get("id") == budget.id), None) if budget.id else None
    if existing is not None:
        before = budgets[existing]
        budgets[existing] = record
    else:
        duplicate = next((b for b in budgets if b["scope"] == budget.scope and b["target"] == budget.target), None)
        if duplicate:
            raise HTTPException(status_code=409, detail=f"A budget for {budget.scope} '{budget.target}' already exists")
        before = None
        record["id"] = str(uuid.uuid4())
        budgets.append(record)

    save_json(config.BUDGETS_FILE, budgets)

    store = load_tags()
    annotations.log_event(store, "budget_update" if before else "budget_create",
                          budget=record, before=before)
    save_tags(store)
    return record


@router.delete("/api/budgets/{budget_id}")
async def delete_budget(budget_id: str):
    budgets = load_json(config.BUDGETS_FILE, [])
    removed = next((b for b in budgets if b.get("id") == budget_id), None)
    if removed is None:
        raise HTTPException(status_code=404, detail="Budget not found")
    save_json(config.BUDGETS_FILE, [b for b in budgets if b.get("id") != budget_id])

    store = load_tags()
    annotations.log_event(store, "budget_delete", budget=removed)
    save_tags(store)
    return {"success": True}


@router.get("/api/budgets/status")
async def get_budget_status(month: Optional[str] = None):
    flow = _flow()
    month = _valid_month(month)
    status = analytics.budget_status(flow, load_json(config.BUDGETS_FILE, []), month)

    # A budget is only as trustworthy as the categorisation under it, so the
    # month's uncategorized share travels with every status response.
    m = status["month"]
    period = pd.Period(m, freq='M')
    status["coverage"] = analytics.uncategorized_coverage(
        flow, period.start_time.strftime('%Y-%m-%d'), period.end_time.strftime('%Y-%m-%d'), top_n=5)
    return status


@router.get("/api/budgets/suggestions")
async def get_budget_suggestions(month: Optional[str] = None):
    return analytics.budget_suggestions(_flow(), _valid_month(month))
