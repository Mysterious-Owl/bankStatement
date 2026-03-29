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
TAGS_FILE = os.path.join(config.BASE_DIR, "tags.json")
REMARKS_FILE = os.path.join(config.BASE_DIR, "remarks.json")

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

class ApplyTagsRequest(BaseModel):
    group_key: str
    tags: List[str]
    create_rule: bool = True
    grouping_columns: List[str]
    entity_values: Optional[Dict[str, str]] = None

class TransactionTagRequest(BaseModel):
    txn_id: str
    tags: List[str]
    source: str = "manual"

class CategoryTagsRequest(BaseModel):
    category_id: str
    tags: List[str]

class RemarkUpdateRequest(BaseModel):
    txn_id: str
    remarks: str

# Helper functions
def load_json(filepath, default=[]):
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            return json.load(f)
    return default

def save_json(filepath, data):
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)

# Tag Management Functions
def load_tags():
    """Load tags data structure, create if doesn't exist"""
    if not os.path.exists(TAGS_FILE):
        default_tags = {
            "transaction_tags": {},
            "rule_tag_mappings": [],
            "category_tags": {},
            "tag_index": []
        }
        save_json(TAGS_FILE, default_tags)
        return default_tags
    return load_json(TAGS_FILE, {
        "transaction_tags": {},
        "rule_tag_mappings": [],
        "category_tags": {},
        "tag_index": []
    })

def save_tags(tags_data):
    """Save tags data"""
    save_json(TAGS_FILE, tags_data)

def add_to_tag_index(tags_data, new_tags):
    """Deprecated: Tags are now derived in real-time"""
    pass

def get_transaction_tags(txn_context, tags_data):
    """
    Get tags for a transaction with priority resolution.
    txn_context: dict containing standard columns and extracted_fields
    Returns: (list of tags, primary_source)
    """
    txn_id = txn_context.get('txn_id')
    all_matched_tags = set()
    primary_source = "none"
    
    # 1. Manual tags
    if txn_id in tags_data.get("transaction_tags", {}):
        tag_info = tags_data["transaction_tags"][txn_id]
        all_matched_tags.update(tag_info.get("tags", []))
        if tag_info.get("source") == "manual":
            primary_source = "manual"

    # 2. Rule-based tags (Flexible Criteria)
    rule_tags = []
    for mapping in tags_data.get("rule_tag_mappings", []):
        criteria = mapping.get("criteria", {})
        if not criteria:
            # Legacy support
            rule_id = mapping.get("rule_id")
            entity_values = mapping.get("entity_values", {})
            if rule_id and txn_context.get('rule_id') == rule_id:
                txn_entities = txn_context.get('entity_values', {})
                if all(txn_entities.get(k) == v for k, v in entity_values.items()):
                    rule_tags.extend(mapping.get("tags", []))
            continue

        match = True
        for key, val in criteria.items():
            txn_val = str(txn_context.get(key, ''))
            if txn_val != str(val):
                # Check entity values
                ex_val = str(txn_context.get('entity_values', {}).get(key, ''))
                if ex_val != str(val):
                    match = False
                    break
        if match:
            rule_tags.extend(mapping.get("tags", []))

    if rule_tags:
        all_matched_tags.update(rule_tags)
        if primary_source == "none": primary_source = "rule"

    # 3. Category tags
    category_id = txn_context.get('category_id') or txn_context.get('category')
    if category_id and category_id in tags_data.get("category_tags", {}):
        cat_tags = tags_data["category_tags"][category_id]
        all_matched_tags.update(cat_tags)
        if primary_source == "none": primary_source = "category"
    
    return sorted(list(all_matched_tags)), primary_source

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

@app.get("/tags")
async def read_tags():
    return FileResponse(os.path.join(STATIC_DIR, 'tags.html'))

@app.get("/category-tags")
async def read_category_tags():
    return FileResponse(os.path.join(STATIC_DIR, 'category-tags.html'))

@app.get("/data")
async def read_data_page():
    return FileResponse(os.path.join(STATIC_DIR, 'data.html'))

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
    
    tags_data = load_tags()
    
    # Convert DataFrame to list of dicts with ISO dates
    base_data = json.loads(df.to_json(orient='records', date_format='iso'))
    
    # Add tags to each transaction
    for i, txn in enumerate(base_data):
        # We need the original row for get_transaction_tags parameters
        original_row = df.iloc[i]
        txn_id = str(original_row.get('TxnID', i))
        
        ctx = {
            "txn_id": txn_id,
            "rule_id": original_row.get('matched_rule'),
            "category_id": original_row.get('matched_category'),
            "entity_values": original_row.get('extracted_fields', {}),
            "bank": original_row.get('Bank'),
            "Description": original_row.get('Description'),
            "rule": original_row.get('matched_rule'),
            "category": original_row.get('matched_category'),
            "source_file": original_row.get('SourceFile')
        }
        
        tags, source = get_transaction_tags(ctx, tags_data)
        
        txn['tags'] = tags
        txn['tag_source'] = source
        # Ensure txn_id is in the response
        txn['txn_id'] = txn_id
    
    return base_data

@app.get("/data")
async def read_data():
    return FileResponse(os.path.join(STATIC_DIR, 'data.html'))

@app.post("/api/transactions/remarks")
async def update_remarks(request: RemarkUpdateRequest):
    # Load manual remarks
    remarks_data = load_json(REMARKS_FILE, {})
    
    # Update remark
    remarks_data[request.txn_id] = request.remarks
    
    # Save back
    save_json(REMARKS_FILE, remarks_data)
        
    # Invalidate cache
    global _enriched_cache
    _enriched_cache["data"] = None
    
    return {"success": True, "remarks": request.remarks}

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

# In-memory cache for enriched data
_enriched_cache = {
    "data": None,
    "last_mtime": 0,
    "rules_mtime": 0
}

def get_enriched_data_df():
    data_path = config.MEGA_STATEMENT_CSV
    rules_path = RULES_FILE
    
    if not os.path.exists(data_path):
        return pd.DataFrame()
        
    data_mtime = os.path.getmtime(data_path)
    rules_mtime = os.path.getmtime(rules_path) if os.path.exists(rules_path) else 0
    
    global _enriched_cache
    if (_enriched_cache["data"] is not None and 
        _enriched_cache["last_mtime"] == data_mtime and 
        _enriched_cache["rules_mtime"] == rules_mtime):
        return _enriched_cache["data"]

    df = pd.read_csv(data_path)
    rules = load_json(RULES_FILE, [])
    categories = load_json(CATEGORIES_FILE, [])
    tags_data = load_tags()
    all_manual_remarks = load_json(REMARKS_FILE, {})
    
    category_map = {c['id']: c for c in categories}
    
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
        except: continue
    
    compiled_rules.sort(key=lambda x: x['priority'], reverse=True)
    
    def enrich_row(row, idx):
        desc = str(row.get('Description', ''))
        matched_rule = None
        extracted = {}
        for cr in compiled_rules:
            match = cr['pattern'].search(desc)
            if match:
                matched_rule = cr
                extracted = match.groupdict() if match.groupdict() else {f'group_{i+1}': g for i, g in enumerate(match.groups())}
                break
        
        rule_name = None
        cat_name = 'Uncategorized'
        cat_color = '#94a3b8'
        rule_id = None
        cat_id = None
        
        if matched_rule:
            rule_id = matched_rule['id']
            rule_name = matched_rule['name']
            cat_id = matched_rule['category_id']
            cat = category_map.get(cat_id, {})
            cat_name = cat.get('name', 'Uncategorized')
            cat_color = cat.get('color', '#94a3b8')
        
        # Add tags
        txn_id = str(row.get('TxnID', idx))
        ctx = {
            "txn_id": txn_id,
            "rule_id": rule_id,
            "category_id": cat_id,
            "entity_values": extracted,
            "bank": row.get('Bank'),
            "Description": desc,
            "rule": rule_name,
            "category": cat_name,
            "source_file": row.get('SourceFile')
        }
        tags, source = get_transaction_tags(ctx, tags_data)
        
        # Get Manual Remarks
        manual_remark = all_manual_remarks.get(txn_id, '')
        
        return rule_name, cat_name, cat_color, extracted, tags, manual_remark

    # Apply enrichment to dataframe
    if not df.empty:
        results = [enrich_row(r, i) for i, r in df.iterrows()]
        df['matched_rule'] = [r[0] for r in results]
        df['matched_category_color'] = [r[2] for r in results]
        df['extracted_fields'] = [r[3] for r in results]
        df['tags'] = [r[4] for r in results]
        df['manual_remarks'] = [r[5] for r in results]
    else:
        df['matched_rule'] = []
        df['matched_category'] = []
        df['matched_category_color'] = []
        df['extracted_fields'] = []
        df['tags'] = []
        df['manual_remarks'] = []
    
    # Ensure Date is datetime
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
    
    # Update cache
    _enriched_cache["data"] = df
    _enriched_cache["last_mtime"] = data_mtime
    _enriched_cache["rules_mtime"] = rules_mtime
    
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
    
    # Tag Breakdown
    tags_all = []
    for tags in filtered_df['tags']:
        if isinstance(tags, list):
            tags_all.extend(tags)
    
    tag_counts = {}
    if tags_all:
        tag_series = pd.Series(tags_all)
        tag_counts = tag_series.value_counts().head(10).to_dict()

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
        "tags": {
            "labels": list(tag_counts.keys()),
            "values": list(tag_counts.values())
        },
        "banks": sorted(df['Bank'].unique().tolist())
    }

# Tag API Endpoints
@app.get("/api/tags/grouped")
async def get_grouped_transactions(columns: Optional[str] = "rule,category"):
    """
    Group transactions by selected columns and return aggregated view with tag info
    columns: comma-separated list like "rule,category,upi_id"
    """
    grouping_cols = [c.strip() for c in columns.split(",")]
    
    df = get_enriched_data_df()
    if df.empty:
        return []
    
    tags_data = load_tags()
    rules = load_json(RULES_FILE, [])
    categories = load_json(CATEGORIES_FILE, [])
    
    # Create lookup maps
    rule_map = {r['id']: r for r in rules}
    category_map = {c['id']: c for c in categories}
    
    grouped_data = []
    
    # Group by specified columns
    for idx, row in df.iterrows():
        group_key_parts = []
        entity_values = {}
        
        rule_id = None
        category_id = None
        
        for col in grouping_cols:
            if col == "rule":
                rule_id = row.get('matched_rule')
                group_key_parts.append(f"rule:{rule_id}")
            elif col == "category":
                category_id = row.get('matched_category')
                group_key_parts.append(f"category:{category_id}")
            elif col == "bank":
                group_key_parts.append(f"bank:{row.get('Bank', '')}")
            elif col == "source_file":
                group_key_parts.append(f"source:{row.get('SourceFile', '')}")
            else:
                # Assume it's an entity field
                extracted = row.get('extracted_fields', {})
                if isinstance(extracted, dict) and col in extracted:
                    val = extracted[col]
                    entity_values[col] = val
                    group_key_parts.append(f"{col}:{val}")
        
        group_key = "|".join(group_key_parts)
        
        # Use TxnID from data
        txn_id = str(row.get('TxnID', idx))
        
        # Build full context for tag resolution (independent of grouping)
        ctx = {
            "txn_id": txn_id,
            "rule_id": row.get('matched_rule'),
            "category_id": row.get('matched_category'),
            "entity_values": row.get('extracted_fields', {}),
            "bank": row.get('Bank'),
            "Description": row.get('Description'),
            "rule": row.get('matched_rule'),
            "category": row.get('matched_category'),
            "source_file": row.get('SourceFile')
        }
        
        # Get tags for this transaction
        tags, source = get_transaction_tags(ctx, tags_data)
        
        # Check if group already exists
        existing_group = next((g for g in grouped_data if g['group_key'] == group_key), None)
        
        if existing_group:
            existing_group['count'] += 1
            existing_group['txn_ids'].append(txn_id)
            # Aggregate tags
            for tag in tags:
                if tag in existing_group['tag_counts']:
                    existing_group['tag_counts'][tag] += 1
                else:
                    existing_group['tag_counts'][tag] = 1
        else:
            # Initialize tag counts
            tag_counts = {}
            for tag in tags:
                tag_counts[tag] = 1
            
            new_group = {
                'group_key': group_key,
                'rule': row.get('matched_rule'),
                'category': row.get('matched_category'),
                'category_color': row.get('matched_category_color'),
                'bank': row.get('Bank'),
                'source_file': row.get('SourceFile'),
                'entity_values': entity_values,
                'count': 1,
                'txn_ids': [txn_id],
                'tags': tags,
                'tag_source': source,
                'tag_counts': tag_counts
            }
            grouped_data.append(new_group)
    
    return grouped_data

@app.post("/api/tags/apply")
async def apply_tags(request: ApplyTagsRequest):
    """Apply tags to a group of transactions using flexible criteria"""
    tags_data = load_tags()
    
    # Parse group_key to extract criteria
    parts = request.group_key.split("|")
    criteria = {}
    
    for part in parts:
        if ":" in part:
            key, val = part.split(":", 1)
            # Standardize keys to match txn context
            if key == "source": key = "source_file"
            criteria[key] = val
    
    # If create_rule is True, create mapping using the criteria
    if request.create_rule and criteria:
        # Check if mapping already exists with exact same criteria
        existing_mapping = next((m for m in tags_data.get("rule_tag_mappings", [])
                                if m.get("criteria") == criteria), None)
        
        old_tags = existing_mapping["tags"] if existing_mapping else []
        removed_tags = set(old_tags) - set(request.tags)
        
        if existing_mapping:
            existing_mapping["tags"] = request.tags
        else:
            new_mapping = {
                "id": str(uuid.uuid4()),
                "criteria": criteria,
                "tags": request.tags,
                "created_at": pd.Timestamp.now().isoformat()
            }
            tags_data.setdefault("rule_tag_mappings", []).append(new_mapping)

        # If tags were removed, also clear them from manual transaction overrides in this group
        if removed_tags:
            # We need to find all transactions matching the criteria to clear their manual tags
            df = get_enriched_data_df()
            if not df.empty:
                # Build filter
                mask = pd.Series(True, index=df.index)
                for key, val in criteria.items():
                    if key == "rule": mask &= (df['matched_rule'] == val)
                    elif key == "category": mask &= (df['matched_category'] == val)
                    elif key == "bank": mask &= (df['Bank'] == val)
                    elif key == "source_file": mask &= (df['SourceFile'] == val)
                    else:
                        # Entity field check
                        mask &= df['extracted_fields'].apply(lambda x: x.get(key) == val if isinstance(x, dict) else False)
                
                matching_txn_ids = [str(df.iloc[i].get('TxnID', i)) for i in df[mask].index]
                for tid in matching_txn_ids:
                    if tid in tags_data.get("transaction_tags", {}):
                        t_tags = tags_data["transaction_tags"][tid].get("tags", [])
                        new_t_tags = [t for t in t_tags if t not in removed_tags]
                        if len(new_t_tags) != len(t_tags):
                            tags_data["transaction_tags"][tid]["tags"] = new_t_tags
    
    save_tags(tags_data)
    return {"success": True, "message": "Tags applied successfully"}

@app.post("/api/tags/transaction")
async def update_transaction_tags(request: TransactionTagRequest):
    """Update tags for a specific transaction (manual override)"""
    tags_data = load_tags()
    
    # Add tags to index
    add_to_tag_index(tags_data, request.tags)
    
    # Get existing tags or create new entry
    existing_tags = []
    if request.txn_id in tags_data.get("transaction_tags", {}):
        existing_tags = tags_data["transaction_tags"][request.txn_id].get("tags", [])
    
    # Merge tags (avoid duplicates)
    all_tags = list(set(existing_tags + request.tags))
    
    # Store transaction tags
    tags_data.setdefault("transaction_tags", {})[request.txn_id] = {
        "tags": all_tags,
        "source": request.source,
        "applied_at": pd.Timestamp.now().isoformat()
    }
    
    save_tags(tags_data)
    
    return {"success": True, "tags": all_tags}

@app.get("/api/tags/autocomplete")
async def get_tag_autocomplete():
    """Get list of all tags for autocomplete derived in real-time"""
    tags_data = load_tags()
    all_tags = set()
    
    # From manual overrides
    for txn in tags_data.get("transaction_tags", {}).values():
        all_tags.update(txn.get("tags", []))
        
    # From rule mappings
    for mapping in tags_data.get("rule_tag_mappings", []):
        all_tags.update(mapping.get("tags", []))
        
    # From category tags
    for tags in tags_data.get("category_tags", {}).values():
        all_tags.update(tags)
        
    return sorted(list(all_tags))

@app.get("/api/tags/details/{group_key:path}")
async def get_group_details(group_key: str):
    """Get all transactions for a specific group"""
    df = get_enriched_data_df()
    if df.empty:
        return []
    
    tags_data = load_tags()
    
    # Parse group_key to filter transactions
    parts = group_key.split("|")
    filter_conditions = {}
    entity_filters = {}
    
    for part in parts:
        if ":" in part:
            key, val = part.split(":", 1)
            if key == "rule":
                filter_conditions['matched_rule'] = val
            elif key == "category":
                filter_conditions['matched_category'] = val
            elif key == "bank":
                filter_conditions['Bank'] = val
            elif key == "source":
                filter_conditions['SourceFile'] = val
            else:
                # It's an entity field
                entity_filters[key] = val
    
    # Filter dataframe by standard columns
    filtered = df.copy()
    for col, val in filter_conditions.items():
        if col in filtered.columns:
            filtered = filtered[filtered[col] == val]
    
    # Filter by entity fields
    if entity_filters:
        def matches_entities(row):
            extracted = row.get('extracted_fields', {})
            if not isinstance(extracted, dict):
                return False
            for key, val in entity_filters.items():
                if extracted.get(key) != val:
                    return False
            return True
        
        filtered = filtered[filtered.apply(matches_entities, axis=1)]
    
    # Convert to list of transactions with tags
    transactions = []
    for idx, row in filtered.iterrows():
        # Use TxnID from data instead of row index
        txn_id = str(row.get('TxnID', idx))
        
        ctx = {
            "txn_id": txn_id,
            "rule_id": row.get('matched_rule'),
            "category_id": row.get('matched_category'),
            "entity_values": row.get('extracted_fields', {}),
            "bank": row.get('Bank'),
            "Description": row.get('Description'),
            "rule": row.get('matched_rule'),
            "category": row.get('matched_category')
        }
        
        tags, source = get_transaction_tags(ctx, tags_data)
        
        txn = json.loads(row.to_json())
        txn['txn_id'] = txn_id  # Use consistent txn_id field
        txn['tags'] = tags
        txn['tag_source'] = source
        transactions.append(txn)
    
    return transactions

@app.get("/api/tags/category")
async def get_category_tags():
    """Get all category tags"""
    tags_data = load_tags()
    return tags_data.get("category_tags", {})

@app.post("/api/tags/category")
async def set_category_tags(request: CategoryTagsRequest):
    """Set default tags for a category"""
    tags_data = load_tags()
    
    # Add tags to index
    add_to_tag_index(tags_data, request.tags)
    
    # Set category tags
    tags_data.setdefault("category_tags", {})[request.category_id] = request.tags
    
    save_tags(tags_data)
    
    return {"success": True}

@app.post("/api/tags/clear")
async def clear_tags(group_key: Optional[str] = None, txn_ids: Optional[List[str]] = None):
    """Clear tags for specified transactions or groups"""
    tags_data = load_tags()
    
    if txn_ids:
        # Clear specific transaction tags
        for txn_id in txn_ids:
            if txn_id in tags_data.get("transaction_tags", {}):
                del tags_data["transaction_tags"][txn_id]
    
    if group_key:
        # Parse group_key and remove corresponding rule mapping
        parts = group_key.split("|")
        rule_id = None
        entity_values = {}
        
        for part in parts:
            if ":" in part:
                key, val = part.split(":", 1)
                if key == "rule":
                    rule_id = val
                elif key not in ["category", "bank", "source"]:
                    entity_values[key] = val
        
        # Remove matching rule mapping
        if rule_id:
            tags_data["rule_tag_mappings"] = [
                m for m in tags_data.get("rule_tag_mappings", [])
                if not (m.get("rule_id") == rule_id and m.get("entity_values") == entity_values)
            ]
    
    save_tags(tags_data)
    
    return {"success": True}

if __name__ == "__main__":
    print(f"Server running at http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
