# 🏗️ Bank Statement Analytics: Architecture Overview

This project is built from the ground up to handle the messy reality of bank statement extraction and normalization.

## 🔄 Core Data Flow

The system operates in four distinct phases:

### Phase 1: Statement Ingestion (`/api/upload`)
*   Files (XLSX, XLS, CSV) are uploaded to a temporary session folder.
*   The system uses **Heuristics** to discover headers:
    *   `src/lib/parser.py` scans rows to find the most "filled" row (highest density) which usually corresponds to the header row.
    *   Forward-filling merges empty/merged header cells.
*   The user reviews suggested **Column Mappings** (Date, Desc, Debit, Credit, etc.).

### Phase 2: The Normalization Pipeline (`pipeline.py`)
*   **Cleaning Logic**:
    *   `find_table_start`: Skips metadata (Account Holder, Address) and locates the actual transaction grid.
    *   `find_table_end`: Detects footers (Total, Closing Balance, Page No) to cut off the dataframe.
    *   `remove_junk_rows`: Cleans out asterisk separators and non-transaction lines.
    *   `Normalization`: Converts all dates into ISO format and money fields into signed floats.
*   **MD5 Deduplication**: Each transaction generates a unique ID based on its content (Date, Desc, Amount, Balance), preventing duplicate rows if statement files overlap.

### Phase 3: The Reconciliation Check (`reconcile.py`)
*   **Mathematical Integrity**:
    *   The system cross-checks every row: `Balance[i] == Balance[i-1] + Credit - Debit`.
    *   It identifies if a statement is "Ascending" (oldest first) or "Descending" (newest first) to correctly sort and validate the timeline.
    *   Large discrepancies are flagged as extraction errors.

### Phase 4: Dynamic Enrichment (`server.py`)
*   **Rules Engine**: Applies user-defined regexes to categorize transactions (e.g., `^UPI/PAYTM.*` -> `Paytm Payment`).
*   **Manual Persistence**:
    *   **Tags**: Manual and criteria-based tags are stored in `tags.json`.
    *   **Remarks**: Manual user notes are stored in `remarks.json`.
    *   These are **Joined in Memory** during the API call, so they survive even if the underlying `MEGA_STATEMENT_CSV` is rebuilt by a new upload.

## 📡 Tech Stack Details

- **Backend**: Python 3.10+, FastAPI (Asynchronous API), Pandas (Data processing), Parquet (High-performance storage).
- **Frontend**: Vanilla JS (Dynamic rendering), Tailwind CSS (Aesthetics), Chart.js (Visualization).
- **Communication**: JSON-based REST APIs with Pydantic for request validation.

## 💾 Storage Schemas

### Enrichment JSONs (`tags.json`, `remarks.json`)
Persistent manual modifications are keyed by `TxnID`, ensuring that as long as the content of the transaction remains the same, the manual note or tag remains attached.

### Statement CSV (`MEGA_STATEMENT_CSV`)
The final source of truth for all combined transactions across all financial institutions.
- **Columns**: Date, Description, Debit, Credit, Balance, RefNumber, TxnType, SourceFile, Bank, TxnID, SystemRemarks.
