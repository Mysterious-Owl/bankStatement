"""
Shared test setup.

Every test runs against a throwaway directory: the `sandbox` fixture points
every path in `config` at pytest's tmp_path before anything reads a file, so
the suite can never touch the real data/ folder. It also clears the module-level
state the app keeps between requests (the enriched-frame cache, pending
uploads), so tests can't leak into each other.
"""
import os
import sys

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)

import config  # noqa: E402

REAL_DATA_DIR = os.path.normcase(os.path.abspath(config.DATA_DIR))

# Every config attribute that names a file or directory under data/.
_DIRS = {
    "DATA_DIR": "",
    "CONFIG_DIR": "config",
    "STATEMENTS_DIR": "statements",
    "RAW_DIR": "statements/raw",
    "ENRICHED_DIR": "statements/enriched",
    "FINAL_DIR": "statements/final",
    "UPLOAD_DIR": "uploads",
    "TXN_METADATA_DIR": "txn_metadata",
    "EXTRACTION_DIR": "statements/extracted",
}
_FILES = {
    "PASS_FILE": "config/passwords.txt",
    "MAPPING_FILE": "config/column_mapping.csv",
    "RULES_FILE": "config/rules.json",
    "CATEGORIES_FILE": "config/categories.json",
    "TAGS_FILE": "config/tags.json",
    "REMARKS_FILE": "config/remarks.json",
    "ANNOTATIONS_FILE": "config/annotations.json",
    "BANKS_FILE": "config/bank_names.csv",
    "BUDGETS_FILE": "config/budgets.json",
    "SELF_TRANSFERS_FILE": "txn_metadata/self_transfers.json",
    "MEGA_STATEMENT_CSV": "statements/enriched/mega_statement.csv",
    "DUPLICATES_REPORT_CSV": "statements/enriched/duplicates_report.csv",
}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """config repointed at an empty temporary data directory."""
    for name, rel in _DIRS.items():
        path = tmp_path / rel
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, name, str(path))
    for name, rel in _FILES.items():
        monkeypatch.setattr(config, name, str(tmp_path / rel))

    # Belt and braces: fail loudly if any path still points at the real data.
    for name in list(_DIRS) + list(_FILES):
        value = os.path.normcase(os.path.abspath(getattr(config, name)))
        assert not value.startswith(REAL_DATA_DIR), f"config.{name} still points at real data"

    from api import enrichment, routes_ingest
    enrichment.invalidate_enriched_cache()
    monkeypatch.setattr(routes_ingest, "pending_uploads", {})
    yield config
    enrichment.invalidate_enriched_cache()


@pytest.fixture
def client(sandbox):
    from fastapi.testclient import TestClient
    import server
    return TestClient(server.app)


@pytest.fixture
def ledger(sandbox):
    """The planted synthetic ledger (see tests/ledger.py)."""
    import ledger as builder
    return builder.build(sandbox)
