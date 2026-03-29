# 📡 API Specification: Bank Statement Analytics

The Backend is a FastAPI application that serves as the controller for the financial ingestion and enrichment engine.

## 🏁 Page Routes
- `/`: Serves the `dashboard.html`.
- `/upload`: Serves the statement upload and mapping page.
- `/data`: Serves the transaction ledger/data grid.
- `/rules`: Serves the regex-based auto-categorization configuration.
- `/tags`: Serves the tagging management interface.
- `/reconcile`: Serves the math-based financial integrity report.

## 📊 Analytics & Data APIs

### `GET /api/dashboard/summary`
Returns high-level statistics (Total Cr/Dr, Net Flow, Count) and visualization data (Income vs. Expense, Cash Availability, Category Distribution).
- **Parameters**: `start_date`, `end_date`, `banks` (comma-separated list).

### `GET /api/transactions/enriched`
Returns all transactions from the Mega Statement with auto-categorization, manual tags, and manual remarks attached.

### `POST /api/transactions/remarks`
Updates the persistent manual remark for a specific transaction ID.
- **Payload**: `{ "txn_id": "md5_hash", "remarks": "User note" }`

## 🧪 Categorization & Rules APIs

### `GET /api/categories`
Returns the list of current financial categories with their color and icon settings.

### `POST /api/rules`
Creates or updates an auto-categorization rule.
- **Payload**: `{ "id": "uuid", "name": "Rule Name", "pattern": "regex", "category_id": "cat_id" }`

### `POST /api/rules/test`
Validates a regex pattern against the entire transaction ledger before saving. Returns matched rows.

## 📥 Statement Ingestion APIs

### `POST /api/upload`
Uploads raw statements, stores them in temporary session folders, and returns candidate column mappings for each file.
- **Form Data**: `files` (List of UploadFile), `bank_names` (List of Strings).

### `POST /api/process-upload`
Confirms the column mappings and finalizes the ingestion pipeline.
- **Payload**: `{ "upload_id": "uuid", "mappings": { "filename": { "raw_col": "std_col" } }, "banks": { "filename": "bank_name" } }`
- **Action**: Triggers `pipeline.py`, cleans statements, deduplicates, and re-calculates the global financial reconciliation.

## 🏷️ Tagging APIs

### `POST /api/tags/transaction`
Applies manual tags to a specific transaction.
- **Payload**: `{ "txn_id": "id", "tags": ["Tag1", "Tag2"], "source": "manual" }`

### `POST /api/tags/apply`
Applies tags to an entire group of transactions matching a specific criteria (e.g., all transactions from a certain bank or file).
- **Payload**: `{ "group_key": "rule:id|bank:name", "tags": ["Tag1"], "create_rule": true }`
