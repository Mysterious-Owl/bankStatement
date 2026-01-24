from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional
import uvicorn
import os
import json
import re
import math
import pandas as pd
import config

app = FastAPI()

# Mount static directory for dashboard (if we had multiple files, but for now just one)
# We will create the dashboard.html in src/static/
STATIC_DIR = os.path.join(config.SRC_DIR, "static")
if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Data files
CATEGORIES_FILE = os.path.join(config.BASE_DIR, "categories.json")
RULES_FILE = os.path.join(config.BASE_DIR, "rules.json")

# Pydantic Models
class Category(BaseModel):
    id: str
    name: str
    color: str = "#3b82f6"
    icon: Optional[str] = None

class Rule(BaseModel):
    id: str
    name: str
    pattern: str
    category_id: str
    priority: int = 0
    extraction_fields: Optional[List[str]] = []

class TestRuleRequest(BaseModel):
    pattern: str

# Helper functions
def load_json(filepath, default=[]):
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            return json.load(f)
    return default

def save_json(filepath, data):
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)

# Page Routes
@app.get("/")
async def read_index():
    return FileResponse(os.path.join(STATIC_DIR, 'dashboard.html'))

@app.get("/reconcile")
async def read_recon():
    return FileResponse(os.path.join(STATIC_DIR, 'reconcile.html'))

@app.get("/rules")
async def read_rules():
    return FileResponse(os.path.join(STATIC_DIR, 'rules.html'))

@app.get("/data.csv")
async def get_data():
    if os.path.exists(config.MEGA_STATEMENT_CSV):
        return FileResponse(config.MEGA_STATEMENT_CSV)
    return {"error": "Data file not found"}

# Category API
@app.get("/api/categories")
async def get_categories():
    return load_json(CATEGORIES_FILE, [])

@app.post("/api/categories")
async def create_category(category: Category):
    categories = load_json(CATEGORIES_FILE, [])
    categories.append(category.dict())
    save_json(CATEGORIES_FILE, categories)
    return category

@app.delete("/api/categories/{category_id}")
async def delete_category(category_id: str):
    categories = load_json(CATEGORIES_FILE, [])
    categories = [c for c in categories if c['id'] != category_id]
    save_json(CATEGORIES_FILE, categories)
    return {"success": True}

# Rules API
@app.get("/api/rules")
async def get_rules():
    return load_json(RULES_FILE, [])

@app.post("/api/rules")
async def create_rule(rule: Rule):
    rules = load_json(RULES_FILE, [])
    # Update if exists, otherwise append
    existing_idx = next((i for i, r in enumerate(rules) if r['id'] == rule.id), None)
    if existing_idx is not None:
        rules[existing_idx] = rule.dict()
    else:
        rules.append(rule.dict())
    save_json(RULES_FILE, rules)
    return rule

@app.delete("/api/rules/{rule_id}")
async def delete_rule(rule_id: str):
    rules = load_json(RULES_FILE, [])
    rules = [r for r in rules if r['id'] != rule_id]
    save_json(RULES_FILE, rules)
    return {"success": True}

# Test Rule Against Transactions
@app.post("/api/rules/test")
async def test_rule(request: TestRuleRequest):
    if not os.path.exists(config.MEGA_STATEMENT_CSV):
        raise HTTPException(status_code=404, detail="Transactions file not found")
    
    try:
        pattern = re.compile(request.pattern, re.IGNORECASE)
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"Invalid regex: {str(e)}")
    
    df = pd.read_csv(config.MEGA_STATEMENT_CSV)
    
    matches = []
    for idx, row in df.iterrows():
        desc = str(row.get('Description', ''))
        match = pattern.search(desc)
        if match:
            matches.append({
                'index': int(idx),
                'date': row.get('Date'),
                'description': desc,
                'debit': row.get('Debit'),
                'credit': row.get('Credit'),
                'bank': row.get('Bank'),
                'extracted': match.groupdict() if match.groupdict() else list(match.groups())
            })
    
    return {
        'count': len(matches),
        'matches': matches[:100]  # Limit to first 100 for performance
    }

# Get transactions by rule
@app.get("/api/transactions/by-rule/{rule_id}")
async def get_transactions_by_rule(rule_id: str):
    rules = load_json(RULES_FILE, [])
    rule = next((r for r in rules if r['id'] == rule_id), None)
    
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    
    try:
        pattern = re.compile(rule['pattern'], re.IGNORECASE)
    except:
        raise HTTPException(status_code=400, detail="Invalid regex in rule")
    
    df = pd.read_csv(config.MEGA_STATEMENT_CSV)
    
    matches = []
    for idx, row in df.iterrows():
        desc = str(row.get('Description', ''))
        match = pattern.search(desc)
        if match:
            matches.append({
                'index': int(idx),
                'date': row.get('Date'),
                'description': desc,
                'debit': row.get('Debit'),
                'credit': row.get('Credit'),
                'bank': row.get('Bank'),
                'category': row.get('Category'),
                'extracted': match.groupdict() if match.groupdict() else list(match.groups())
            })
    
    return matches

# Get unassigned transactions (not matching any rule)
@app.get("/api/transactions/unassigned")
async def get_unassigned_transactions():
    rules = load_json(RULES_FILE, [])
    df = pd.read_csv(config.MEGA_STATEMENT_CSV)
    
    # Compile all patterns
    patterns = []
    for rule in rules:
        try:
            patterns.append(re.compile(rule['pattern'], re.IGNORECASE))
        except:
            continue
    
    unassigned = []
    for idx, row in df.iterrows():
        desc = str(row.get('Description', ''))
        matched = any(p.search(desc) for p in patterns)
        
        if not matched:
            unassigned.append({
                'index': int(idx),
                'date': row.get('Date'),
                'description': desc,
                'debit': row.get('Debit'),
                'credit': row.get('Credit'),
                'bank': row.get('Bank'),
                'category': row.get('Category')
            })
    
    return unassigned

# Get transactions with conflicts (matching multiple rules)
@app.get("/api/transactions/conflicts")
async def get_conflicts():
    rules = load_json(RULES_FILE, [])
    df = pd.read_csv(config.MEGA_STATEMENT_CSV)
    
    # Compile all patterns with rule info
    compiled_rules = []
    for rule in rules:
        try:
            compiled_rules.append({
                'id': rule['id'],
                'name': rule['name'],
                'pattern': re.compile(rule['pattern'], re.IGNORECASE),
                'priority': rule.get('priority', 0)
            })
        except:
            continue
    
    conflicts = []
    for idx, row in df.iterrows():
        desc = str(row.get('Description', ''))
        matched_rules = []
        
        for cr in compiled_rules:
            if cr['pattern'].search(desc):
                matched_rules.append({
                    'rule_id': cr['id'],
                    'rule_name': cr['name'],
                    'priority': cr['priority']
                })
        
        if len(matched_rules) > 1:
            conflicts.append({
                'index': int(idx),
                'date': row.get('Date'),
                'description': desc,
                'debit': row.get('Debit'),
                'credit': row.get('Credit'),
                'bank': row.get('Bank'),
                'matched_rules': matched_rules
            })
    
    return conflicts

# Get all transactions with rules applied
@app.get("/api/transactions/enriched")
async def get_enriched_transactions():
    rules = load_json(RULES_FILE, [])
    categories = load_json(CATEGORIES_FILE, [])
    df = pd.read_csv(config.MEGA_STATEMENT_CSV)
    
    # Create category lookup
    category_map = {c['id']: c for c in categories}
    
    # Compile all patterns with rule info
    compiled_rules = []
    for rule in rules:
        try:
            compiled_rules.append({
                'id': rule['id'],
                'name': rule['name'],
                'pattern': re.compile(rule['pattern'], re.IGNORECASE),
                'priority': rule.get('priority', 0),
                'category_id': rule.get('category_id')
            })
        except:
            continue
    
    # Sort by priority descending (highest priority first)
    compiled_rules.sort(key=lambda x: x['priority'], reverse=True)
    
    # Helper to convert NaN/Inf to None for JSON compatibility
    def safe_value(val):
        if pd.isna(val):
            return None
        if isinstance(val, (int, float)):
            if not math.isfinite(val):
                return None
        return val
    
    enriched = []
    for idx, row in df.iterrows():
        desc = str(row.get('Description', ''))
        
        # Find matching rules
        matched_rule = None
        extracted = {}
        
        for cr in compiled_rules:
            match = cr['pattern'].search(desc)
            if match:
                matched_rule = cr
                extracted = match.groupdict() if match.groupdict() else {f'group_{i}': g for i, g in enumerate(match.groups())}
                break  # Take first match (highest priority)
        
        # Build enriched record with safe values
        record = {
            'index': int(idx),
            'date': safe_value(row.get('Date')),
            'description': desc,
            'debit': safe_value(row.get('Debit')),
            'credit': safe_value(row.get('Credit')),
            'balance': safe_value(row.get('Balance')),
            'bank': safe_value(row.get('Bank')),
            'category': safe_value(row.get('Category')),
            'entity': safe_value(row.get('Entity')),
            'ref_number': safe_value(row.get('RefNumber')),
            'remarks': safe_value(row.get('Remarks')),
            'source_file': safe_value(row.get('SourceFile'))
        }
        
        # Add rule enrichment
        if matched_rule:
            category = category_map.get(matched_rule['category_id'], {})
            record['matched_rule'] = matched_rule['name']
            record['matched_category'] = category.get('name', '')
            record['matched_category_color'] = category.get('color', '')
            record['extracted_fields'] = extracted
        else:
            record['matched_rule'] = None
            record['matched_category'] = None
            record['matched_category_color'] = None
            record['extracted_fields'] = {}
        
        enriched.append(record)
    
    return enriched

@app.get("/data")
async def read_data():
    return FileResponse(os.path.join(STATIC_DIR, 'data.html'))

if __name__ == "__main__":
    print(f"Server running at http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)

