"""Statement upload, column mapping, and pipeline/reconciliation triggers."""
import json
import os
import shutil
import uuid
from typing import List

import pandas as pd
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

import config
import pipeline
import reconcile
from api import ingest_mapping
from api.models import ProcessUploadRequest
from lib.parser import (find_table_end, find_table_start, get_passwords,
                        remove_junk_rows, try_open_workbook)

router = APIRouter()

MAPPING_COLUMNS = ['FileName', 'Bank', 'RawColumn', 'MappedTo']
HISTORY_EXAMPLES = 3

# Uploads awaiting a mapping decision, keyed by upload_id.
pending_uploads = {}


@router.get("/api/banks")
async def get_banks():
    if not os.path.exists(config.BANKS_FILE):
        return []
    try:
        return pd.read_csv(config.BANKS_FILE).fillna("").to_dict(orient="records")
    except (pd.errors.ParserError, OSError, UnicodeDecodeError):
        return []


@router.get("/api/bank-options")
async def get_bank_options():
    """
    Bank names for the upload picker: KNOWN_BANKS from .env plus every bank
    in the registry, sorted, with "Other" last.
    """
    names = set(config.KNOWN_BANKS)
    if os.path.exists(config.BANKS_FILE):
        try:
            names.update(str(n) for n in pd.read_csv(config.BANKS_FILE)['Name'].dropna() if str(n).strip())
        except (pd.errors.ParserError, KeyError, OSError, UnicodeDecodeError):
            pass
    names.discard("Other")
    return sorted(names) + ["Other"]


def _safe_name(bank, filename, taken):
    """
    The name a statement is stored under: the bank prefixed unless the file
    already carries it, reduced to a bare filename (the client controls it, so
    "../" must not escape the upload folder), and made unique against `taken`
    so a new statement never replaces an existing raw one that happens to
    share its name.
    """
    def bare(name):
        return os.path.basename(str(name).replace('\\', '/')).lstrip('.')

    filename = bare(filename) or "statement"
    if bank.upper() not in filename.upper():
        filename = bare(f"{bank}_{filename}") or filename  # the bank name is client input too

    stem, ext = os.path.splitext(filename)
    candidate, n = filename, 2
    while candidate in taken:
        candidate = f"{stem} ({n}){ext}"
        n += 1
    return candidate


def _read_table(file_path, passwords):
    """Open a statement and trim it down to the actual transaction table."""
    df = try_open_workbook(file_path, passwords)
    if df is None:
        return None
    cleaned, _ = find_table_start(df)
    if cleaned is None:
        return None
    return remove_junk_rows(find_table_end(cleaned))


@router.post("/api/upload")
async def upload_statements(bank_names: str = Form(...), files: List[UploadFile] = File(...)):
    """
    Stage uploaded statements and propose a column mapping for each distinct
    header layout.

    Files that share a header layout are grouped, so the user confirms one
    mapping for a whole batch of look-alike statements rather than one per file.
    """
    bank_list = json.loads(bank_names)
    if len(bank_list) != len(files):
        raise HTTPException(status_code=400, detail="Number of banks does not match number of files")

    upload_id = str(uuid.uuid4())
    upload_path = os.path.join(config.UPLOAD_DIR, upload_id)
    os.makedirs(upload_path, exist_ok=True)

    passwords = get_passwords(config.PASS_FILE)
    saved_files = []
    grouped = {}  # header tuple -> aggregate

    taken = set(os.listdir(config.RAW_DIR)) if os.path.isdir(config.RAW_DIR) else set()
    for bank, upload in zip(bank_list, files):
        filename = _safe_name(bank, upload.filename, taken)
        taken.add(filename)
        file_path = os.path.join(upload_path, filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(upload.file, buffer)
        saved_files.append(file_path)

        table = _read_table(file_path, passwords)
        if table is None:
            continue

        headers = tuple(str(c) for c in table.columns)
        if headers not in grouped:
            grouped[headers] = ingest_mapping.new_header_group(headers, bank, filename, table)
        else:
            grouped[headers]["filenames"].append(filename)
        ingest_mapping.accumulate(grouped[headers], headers, table)

    pending_uploads[upload_id] = {"files": saved_files, "upload_path": upload_path}
    return {"upload_id": upload_id, "candidate_mappings": ingest_mapping.finalize(grouped)}


@router.get("/api/mapping/history/{bank}")
async def get_mapping_history(bank: str):
    """Distinct mappings previously used for this bank, most common first."""
    if not os.path.exists(config.MAPPING_FILE):
        return []

    df = pd.read_csv(config.MAPPING_FILE).fillna('')
    if 'Bank' not in df.columns or 'FileName' not in df.columns:
        return []

    bank_df = df[df['Bank'].str.upper() == bank.upper()]
    if bank_df.empty:
        return []

    configs = {}  # canonical JSON of the mapping -> files that used it
    for filename, group in bank_df.groupby('FileName'):
        mapping = dict(zip(group['RawColumn'], group['MappedTo']))
        config_key = json.dumps(dict(sorted(mapping.items())))
        configs.setdefault(config_key, {"mapping": mapping, "files": []})["files"].append(filename)

    history = [
        {"mapping": cfg["mapping"], "count": len(cfg["files"]),
         "examples": cfg["files"][:HISTORY_EXAMPLES]}
        for cfg in configs.values()
    ]
    history.sort(key=lambda h: h["count"], reverse=True)
    return history


def _persist_mappings(mappings, banks):
    """Replace the stored column mapping for exactly the files just mapped."""
    rows = [
        {"FileName": filename, "Bank": banks.get(filename, "Other"),
         "RawColumn": raw, "MappedTo": standard}
        for filename, mapping in mappings.items()
        for raw, standard in mapping.items()
    ]
    if not rows:
        return

    new_df = pd.DataFrame(rows)
    if not os.path.exists(config.MAPPING_FILE):
        new_df.to_csv(config.MAPPING_FILE, index=False)
        return

    existing = pd.read_csv(config.MAPPING_FILE).fillna('')
    existing = existing[~existing['FileName'].isin(mappings.keys())]
    for column in MAPPING_COLUMNS:
        if column not in existing.columns:
            existing[column] = ""
    pd.concat([existing, new_df], ignore_index=True).to_csv(config.MAPPING_FILE, index=False)


@router.post("/api/process-upload")
async def process_statements(request: ProcessUploadRequest):
    session = pending_uploads.get(request.upload_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Upload session not found")

    _persist_mappings(request.mappings, request.banks)

    for file_path in session['files']:
        shutil.move(file_path, os.path.join(config.RAW_DIR, os.path.basename(file_path)))

    shutil.rmtree(session['upload_path'])
    del pending_uploads[request.upload_id]

    # Re-run over everything, not just the new files: aggregation and
    # deduplication are only correct when computed across the whole corpus.
    return {
        "pipeline_summary": pipeline.run_pipeline(bank_overrides=request.banks),
        "recon_data": reconcile.run_reconciliation(),
    }


@router.get("/api/reconcile/summary")
async def get_reconcile_summary():
    return reconcile.run_reconciliation()
