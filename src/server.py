from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional, Dict
import uvicorn
import os
import json
import re
import math
import shutil
import uuid
import pandas as pd
import config
import pipeline
import reconcile
from lib.parser import get_passwords, try_open_workbook, find_table_start

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

@app.get("/upload")
async def read_upload():
    return FileResponse(os.path.join(STATIC_DIR, 'upload.html'))

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
    df = get_enriched_data_df()
    if df.empty:
        return []
    
    # Convert to list of dicts and handle non-serializable values
    # We use json.loads(df.to_json) for a quick and safe conversion
    return json.loads(df.to_json(orient='records', date_format='iso'))

@app.get("/data")
async def read_data():
    return FileResponse(os.path.join(STATIC_DIR, 'data.html'))

# --- Statement Ingestion APIs ---

# Temporary store for pending uploads
pending_uploads = {}

@app.post("/api/upload")
async def upload_statements(
    bank_names: str = Form(...),
    files: List[UploadFile] = File(...)
):
    upload_id = str(uuid.uuid4())
    upload_path = os.path.join(config.UPLOAD_DIR, upload_id)
    os.makedirs(upload_path, exist_ok=True)
    
    bank_list = json.loads(bank_names)
    if len(bank_list) != len(files):
        raise HTTPException(status_code=400, detail="Number of banks does not match number of files")

    saved_files = []
    candidate_mappings = {}
    
    # Logic from generate_mapping.py
    def suggest_mapping(headers):
        mappings = {}
        for col in headers:
            best_match = ""
            col_str = str(col).lower()
            if "date" in col_str or "value dt" in col_str: best_match = "Date"
            elif "description" in col_str or any(x in col_str for x in ["desc", "particular", "narration", "details", "remarks"]): best_match = "Description"
            elif any(x in col_str for x in ["debit", "dr", "withdrawal"]) and not any(x in col_str for x in ["dr / cr"]): best_match = "Debit"
            elif any(x in col_str for x in ["credit", "cr", "deposit"]) and not any(x in col_str for x in ["dr / cr"]): best_match = "Credit"
            elif "bal" in col_str: best_match = "Balance"
            elif any(x in col_str for x in ["ref", "chq", "txn", "cheque", "reference"]): best_match = "RefNumber"
            elif "type" in col_str: best_match = "TxnType"
            elif "amount" in col_str: best_match = "Amount"
            
            if best_match:
                mappings[col] = best_match
        return mappings

    passwords = get_passwords(config.PASS_FILE)

    for i, file in enumerate(files):
        current_bank = bank_list[i]
        # Sanitize filename: bank_name_original_name
        safe_name = f"{current_bank}_{file.filename}"
        file_path = os.path.join(upload_path, safe_name)
        
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        saved_files.append(file_path)
        
        # Suggest Mapping
        df = try_open_workbook(file_path, passwords)
        if df is not None:
            cleaned_df, _ = find_table_start(df)
            if cleaned_df is not None:
                headers = [str(c) for c in cleaned_df.columns]
                candidate_mappings[safe_name] = {
                    "raw_columns": headers,
                    "mappings": suggest_mapping(headers)
                }

    pending_uploads[upload_id] = {
        "files": saved_files,
        "upload_path": upload_path
    }
    
    return {
        "upload_id": upload_id,
        "candidate_mappings": candidate_mappings
    }

class ProcessUploadRequest(BaseModel):
    upload_id: str
    mappings: Dict[str, Dict[str, str]]
    banks: Dict[str, str]

@app.post("/api/process-upload")
async def process_statements(request: ProcessUploadRequest):
    if request.upload_id not in pending_uploads:
        raise HTTPException(status_code=404, detail="Upload session not found")
        
    session = pending_uploads[request.upload_id]
    
    # 1. Update column_mapping.csv
    new_mapping_rows = []
    for filename, mapping in request.mappings.items():
        for raw, std in mapping.items():
            new_mapping_rows.append({
                "FileName": filename,
                "RawColumn": raw,
                "MappedTo": std
            })
    
    if new_mapping_rows:
        new_map_df = pd.DataFrame(new_mapping_rows)
        if os.path.exists(config.MAPPING_FILE):
             existing_df = pd.read_csv(config.MAPPING_FILE)
             # Remove existing for these files
             existing_df = existing_df[~existing_df['FileName'].isin(request.mappings.keys())]
             combined_df = pd.concat([existing_df, new_map_df], ignore_index=True)
             combined_df.to_csv(config.MAPPING_FILE, index=False)
        else:
             new_map_df.to_csv(config.MAPPING_FILE, index=False)

    # 2. Move files to RAW_DIR
    processed_files = []
    for f_path in session['files']:
        dest = os.path.join(config.RAW_DIR, os.path.basename(f_path))
        shutil.move(f_path, dest)
        processed_files.append(dest)
        
    # 3. Close session & cleanup temp dir
    shutil.rmtree(session['upload_path'])
    del pending_uploads[request.upload_id]
    
    # 4. Run Pipeline on EVERYTHING to ensure consistency and aggregation
    pipeline_summary = pipeline.run_pipeline(bank_overrides=request.banks)
    
    # 5. Run Reconciliation
    recon_data = reconcile.run_reconciliation()
    
    return {
        "pipeline_summary": pipeline_summary,
        "recon_data": recon_data
    }

def get_enriched_data_df():
    data_path = config.MEGA_STATEMENT_CSV
    
    if not os.path.exists(data_path):
        return pd.DataFrame()
        
    df = pd.read_csv(data_path)
    rules = load_json(RULES_FILE, [])
    categories = load_json(CATEGORIES_FILE, [])
    category_map = {c['id']: c for c in categories}
    
    compiled_rules = []
    for rule in rules:
        try:
            compiled_rules.append({
                'name': rule['name'],
                'pattern': re.compile(rule['pattern'], re.IGNORECASE),
                'priority': rule.get('priority', 0),
                'category_id': rule.get('category_id')
            })
        except: continue
    
    compiled_rules.sort(key=lambda x: x['priority'], reverse=True)
    
    def enrich_row(row):
        desc = str(row.get('Description', ''))
        matched_rule = None
        extracted = {}
        for cr in compiled_rules:
            match = cr['pattern'].search(desc)
            if match:
                matched_rule = cr
                extracted = match.groupdict() if match.groupdict() else {f'group_{i+1}': g for i, g in enumerate(match.groups())}
                break
        
        if matched_rule:
            cat = category_map.get(matched_rule['category_id'], {})
            return matched_rule['name'], cat.get('name', 'Uncategorized'), cat.get('color', '#94a3b8'), extracted
        return None, 'Uncategorized', '#94a3b8', {}

    # Apply enrichment to dataframe
    if not df.empty:
        enrichment_results = df.apply(enrich_row, axis=1)
        df['matched_rule'] = [r[0] for r in enrichment_results]
        df['matched_category'] = [r[1] for r in enrichment_results]
        df['matched_category_color'] = [r[2] for r in enrichment_results]
        df['extracted_fields'] = [r[3] for r in enrichment_results]
    else:
        df['matched_rule'] = None
        df['matched_category'] = 'Uncategorized'
        df['matched_category_color'] = '#94a3b8'
        df['extracted_fields'] = [{}]
    
    # Ensure Date is datetime
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
    
    return df

@app.get("/api/dashboard/summary")
async def get_dashboard_summary(start_date: Optional[str] = None, end_date: Optional[str] = None, banks: Optional[str] = None):
    df = get_enriched_data_df()
    if df.empty:
        return {"error": "No data"}
    
    filtered_df = df.copy()
    if start_date:
        filtered_df = filtered_df[filtered_df['Date'] >= pd.to_datetime(start_date)]
    if end_date:
        filtered_df = filtered_df[filtered_df['Date'] <= pd.to_datetime(end_date)]
    if banks:
        bank_list = banks.split(',')
        filtered_df = filtered_df[filtered_df['Bank'].isin(bank_list)]
        
    # Metrics
    total_credit = float(filtered_df['Credit'].sum())
    total_debit = float(filtered_df['Debit'].sum())
    
    # Time Series
    # Daily Income/Expense
    daily = filtered_df.groupby(filtered_df['Date'].dt.date).agg({'Credit': 'sum', 'Debit': 'sum'}).sort_index()
    
    # Cash Availability (Cumulative Balance)
    if not filtered_df.empty:
        all_dates = pd.date_range(filtered_df['Date'].min(), filtered_df['Date'].max())
        unique_banks = filtered_df['Bank'].unique()
        
        bal_matrix = pd.DataFrame(index=all_dates)
        for bank in unique_banks:
            # Get last balance of each day for this bank
            bank_data = filtered_df[filtered_df['Bank'] == bank].groupby(filtered_df['Date'].dt.date)['Balance'].last()
            bank_data.index = pd.to_datetime(bank_data.index)
            bal_matrix[bank] = bank_data
            bal_matrix[bank] = bal_matrix[bank].ffill() 
            bal_matrix[bank] = bal_matrix[bank].fillna(0) 
            
        bal_series = bal_matrix.sum(axis=1)
    else:
        bal_series = pd.Series()

    # Category Breakdown
    cat_breakdown = filtered_df[filtered_df['Debit'] > 0].groupby('matched_category')['Debit'].sum().sort_values(ascending=False)

    return {
        "metrics": {
            "total_credit": total_credit,
            "total_debit": total_debit,
            "net_flow": total_credit - total_debit,
            "txn_count": len(filtered_df)
        },
        "daily": {
            "labels": [d.strftime('%Y-%m-%d') for d in daily.index],
            "credit": daily['Credit'].tolist(),
            "debit": daily['Debit'].tolist()
        },
        "cash_availability": {
            "labels": [d.strftime('%Y-%m-%d') for d in bal_series.index],
            "data": bal_series.tolist()
        },
        "categories": {
            "labels": cat_breakdown.index.tolist(),
            "values": cat_breakdown.tolist()
        },
        "banks": sorted(df['Bank'].unique().tolist())
    }

if __name__ == "__main__":
    print(f"Server running at http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
