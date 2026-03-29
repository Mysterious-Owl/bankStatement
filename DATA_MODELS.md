# 📂 Data Models & Schema Registry

The system uses a simple, local-first data architecture. All data is persisted in CSV, Parquet, and JSON files within the project root.

## 📁 Core Financial Data

### `MEGA_STATEMENT_CSV` (`data/final_statements/mega_statement.csv`)
The primary source of truth for all combined transactions.

| Column | Type | Description |
| :--- | :--- | :--- |
| **Date** | ISO Date | Normalised YYYY-MM-DD format. |
| **Description** | String | Cleaned, concatenated transaction narration. |
| **Debit** | Float | Amount withdrawn (always positive). |
| **Credit** | Float | Amount deposited (always positive). |
| **Balance** | Float | The account balance after the transaction. |
| **RefNumber** | String | Bank reference or Cheque number. |
| **TxnType** | String | Metadata from the bank (e.g., NEFT/RTGS/IMPS). |
| **Bank** | String | The institution name (e.g., SBI, HDFC). |
| **SourceFile** | String | The original statement filename. |
| **TxnID** | MD5 Hash | Unique fingerprint (Date+Desc+Debit+Credit+Balance+RefNo). |
| **SystemRemarks** | String | Auto-detected discrepancies (e.g., date vs. value date). |

---

## 📁 Enrichment Registries (JSON)

### `categories.json`
Stores the styling and metadata for financial categories.
```json
{
  "id": "uuid",
  "name": "Dining",
  "color": "#ef4444",
  "icon": "utensils"
}
```

### `rules.json`
Stores classification logic that maps transaction descriptions to categories.
```json
{
  "id": "uuid",
  "name": "Paytm Rule",
  "pattern": "^PAYTM.*",
  "category_id": " dining_cat_id",
  "priority": 10
}
```

### `tags.json`
Maintains manual and criteria-based transaction tagging.
```json
{
  "transaction_tags": {
    "txn_id_hash": { "tags": ["Business", "Travel"], "source": "manual", "applied_at": "timestamp" }
  },
  "rule_tag_mappings": [
    { "id": "uuid", "criteria": { "rule": "Paytm Rule" }, "tags": ["Digital"] }
  ],
  "category_tags": {
    "dining_cat_id": ["Eats"]
  }
}
```

### `remarks.json`
Simple key-value store for persistent manual user notes.
```json
{
  "txn_id_hash": "Manual user-written note about this transaction."
}
```

---

## 📁 Ingestion Metadata

### `column_mapping.csv`
A registry of how bank-specific headers map to our internal standard.

| FileName | RawColumn | MappedTo |
| :--- | :--- | :--- |
| bank_statement_1.xlsx | Trans Date | Date |
| bank_statement_2.csv | Particulars | Description |
| bank_statement_2.csv | Withdrawal | Debit |
```
