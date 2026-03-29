# 🏦 Financial Statement Analytics WebApp

A comprehensive, private, local-first web application designed to handle, combine, and analyze bank statements from multiple financial institutions (HDFC, SBI, IDFC, ICICI, etc.).

## 🚀 Key Features

*   **Smart Ingestion**:
    *   Handles **XLS, XLSX, and CSV** formats.
    *   Supports **password-protected** Excel files (via `msoffcrypto`).
    *   Automated detection of table headers and footer totals.
*   **Cleaning Pipeline**:
    *   Removes bank-specific junk rows (asterisks, separators).
    *   Identifies indented columns and table starts.
    *   Smart deduplication using MD5 content hashing.
*   **Consolidated Ledger**:
    *   Merges transactions into a single "Mega Statement" (CSV/Parquet).
    *   Time-sorting even for statements with newest-transaction-first order.
*   **Advanced Dashboard**:
    *   **Chart.js** visuals for Income vs. Expense and Cash Availability.
    *   Category and Tag distribution charts.
*   **Persistence**:
    *   **Rules Engine**: Auto-categorization using regex patterns.
    *   **Manual Remarks**: Set persistent notes for any transaction that stay even when re-processing data.
    *   **Reconciliation**: Financial integrity checks to detect extraction errors automatically.

## 🛠️ Project Architecture

*   `src/server.py`: FastAPI backend, transaction enrichment, and API endpoints.
*   `src/pipeline.py`: The core statement extraction and merging engine.
*   `src/lib/parser.py`: Low-level Excel/CSV parsing utilities and heuristics.
*   `src/reconcile.py`: Mathematical validation logic for the transaction ledger.
*   `src/static/`: Premium web interface built with Vanilla JS and Tailwind CSS.
*   `data/`: Storage for raw extracts, enriched statements, and the final ledger.

## 📁 Data Storage (JSON and CSV)

*   `categories.json`: User-defined financial categories and color mappings.
*   `rules.json`: Regex patterns for auto-categorization.
*   `tags.json`: Manual and rule-based transaction tagging registry.
*   `remarks.json`: Persistent user-added remarks for individual transactions.
*   `column_mapping.csv`: Mapping of raw bank headers to the standard internal schema.

## 🚀 Getting Started

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
2.  **Add Passwords**: If your statements are protected, add the passwords to `passwords.txt` (one per line).
3.  **Run the App**:
    ```bash
    python src/server.py
    ```
4.  **Analyze**: Open `http://localhost:8000` to start uploading and analyzing your finances.

---
**Privacy First**: This application runs 100% locally. No financial data ever leaves your computer.
