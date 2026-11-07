"""Categories, rules, and the rule-inspection views built on top of them."""
import os
import re

import pandas as pd
from fastapi import APIRouter, HTTPException

import config
from api import helpers
from jsonio import load_json, save_json
from api.models import Category, Rule, TestRuleRequest

router = APIRouter()

MATCH_PREVIEW_LIMIT = 100


def _ledger():
    """The raw ledger, or an empty frame when nothing has been ingested."""
    if not os.path.exists(config.MEGA_STATEMENT_CSV):
        return pd.DataFrame()
    return pd.read_csv(config.MEGA_STATEMENT_CSV)


# --- Categories -----------------------------------------------------------

@router.get("/api/categories")
async def get_categories():
    return load_json(config.CATEGORIES_FILE, [])


@router.post("/api/categories")
async def create_category(category: Category):
    categories = load_json(config.CATEGORIES_FILE, [])
    categories.append(category.dict())
    save_json(config.CATEGORIES_FILE, categories)
    return category


@router.delete("/api/categories/{category_id}")
async def delete_category(category_id: str):
    categories = [c for c in load_json(config.CATEGORIES_FILE, []) if c['id'] != category_id]
    save_json(config.CATEGORIES_FILE, categories)
    return {"success": True}


# --- Rules ----------------------------------------------------------------

@router.get("/api/rules")
async def get_rules():
    return load_json(config.RULES_FILE, [])


@router.post("/api/rules")
async def create_rule(rule: Rule):
    rules = load_json(config.RULES_FILE, [])
    existing_idx = next((i for i, r in enumerate(rules) if r['id'] == rule.id), None)
    if existing_idx is not None:
        rules[existing_idx] = rule.dict()
    else:
        rules.append(rule.dict())
    save_json(config.RULES_FILE, rules)
    return rule


@router.delete("/api/rules/{rule_id}")
async def delete_rule(rule_id: str):
    rules = [r for r in load_json(config.RULES_FILE, []) if r['id'] != rule_id]
    save_json(config.RULES_FILE, rules)
    return {"success": True}


@router.post("/api/rules/test")
async def test_rule(request: TestRuleRequest):
    if not os.path.exists(config.MEGA_STATEMENT_CSV):
        raise HTTPException(status_code=404, detail="Transactions file not found")

    try:
        pattern = re.compile(request.pattern, re.IGNORECASE)
    except re.error as exc:
        raise HTTPException(status_code=400, detail=f"Invalid regex: {exc}")

    matches = []
    for idx, row in _ledger().iterrows():
        match = pattern.search(str(row.get('Description', '')))
        if match:
            matches.append(helpers.txn_summary(
                idx, row, extracted=helpers.extracted_values(match)))

    return {'count': len(matches), 'matches': matches[:MATCH_PREVIEW_LIMIT]}


# --- Rule inspection ------------------------------------------------------

@router.get("/api/transactions/by-rule/{rule_id}")
async def get_transactions_by_rule(rule_id: str):
    rule = next((r for r in load_json(config.RULES_FILE, []) if r['id'] == rule_id), None)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    compiled = helpers.compile_rules([rule])
    if not compiled:
        raise HTTPException(status_code=400, detail="Invalid regex in rule")
    pattern = compiled[0]['pattern']

    df = _ledger()
    if df.empty:
        return []

    matches = []
    for idx, row in df.iterrows():
        match = pattern.search(str(row.get('Description', '')))
        if match:
            matches.append(helpers.txn_summary(
                idx, row,
                category=row.get('Category'),
                extracted=helpers.extracted_values(match),
            ))
    return matches


@router.get("/api/transactions/unassigned")
async def get_unassigned_transactions():
    """Transactions no rule claims -- the queue of work for the rules page."""
    df = _ledger()
    if df.empty:
        return []

    compiled = helpers.compile_rules(load_json(config.RULES_FILE, []))
    return [
        helpers.txn_summary(idx, row, category=row.get('Category'))
        for idx, row in df.iterrows()
        if not any(r['pattern'].search(str(row.get('Description', ''))) for r in compiled)
    ]


@router.get("/api/transactions/conflicts")
async def get_conflicts():
    """Transactions more than one rule matches; priority decides the winner."""
    df = _ledger()
    if df.empty:
        return []

    compiled = helpers.compile_rules(load_json(config.RULES_FILE, []))

    conflicts = []
    for idx, row in df.iterrows():
        description = str(row.get('Description', ''))
        matched = [
            {'rule_id': r['id'], 'rule_name': r['name'], 'priority': r['priority']}
            for r in compiled if r['pattern'].search(description)
        ]
        if len(matched) > 1:
            conflicts.append(helpers.txn_summary(idx, row, matched_rules=matched))
    return conflicts
