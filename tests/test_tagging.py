"""Annotation keys, tag resolution, the tag APIs, self transfers and orphan recovery."""
import json

import pandas as pd

import annotations


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def row(client, needle):
    return next(t for t in ok(client.get("/api/transactions/enriched")) if needle in t["Description"])


# --- anno_key --------------------------------------------------------------

def test_anno_key_survives_parser_noise():
    base = annotations.compute_anno_key("HDFC", "2024-05-03", 640, 0, "SWIGGY ORDER 88213")
    # The ledger always stores ISO dates; time components and number/whitespace
    # formatting must not change the key.
    assert base == annotations.compute_anno_key("hdfc ", "2024-05-03 00:00:00", "640.0", "", "  swiggy   order 88213 ")
    assert base == annotations.compute_anno_key("HDFC", "2024-05-03T00:00:00", 640.0, None, "SWIGGY ORDER 88213")
    assert base != annotations.compute_anno_key("HDFC", "2024-05-03", 641, 0, "SWIGGY ORDER 88213")
    assert base != annotations.compute_anno_key("ICICI", "2024-05-03", 640, 0, "SWIGGY ORDER 88213")


def test_apply_tag_delta_semantics():
    entry = {"tags": ["a"], "excluded_tags": ["b"]}
    annotations.apply_tag_delta(entry, add=["b", "c"])
    assert entry == {"tags": ["a", "b", "c"], "excluded_tags": []}
    annotations.apply_tag_delta(entry, remove=["a"])
    assert entry == {"tags": ["b", "c"], "excluded_tags": ["a"]}
    annotations.apply_tag_delta(entry, set_to=["a"])
    assert entry == {"tags": ["a"], "excluded_tags": []}


# --- Resolution through the API -------------------------------------------

def test_category_tags_are_inherited(client, ledger):
    ok(client.post("/api/tags/category", json={"category_id": "cat-food", "tags": ["eating-out"]}))
    swiggy = row(client, "SWIGGY ORDER 62929")
    assert swiggy["tags"] == ["eating-out"] and swiggy["tag_sources"] == {"eating-out": "category"}


def test_inherited_tag_can_be_removed_from_one_transaction(client, ledger):
    ok(client.post("/api/tags/category", json={"category_id": "cat-food", "tags": ["eating-out"]}))
    target = row(client, "SWIGGY ORDER 62929")
    ok(client.post("/api/tags/transaction", json={"txn_id": target["anno_key"], "tags": ["eating-out"], "mode": "remove"}))
    food = [t for t in ok(client.get("/api/transactions/enriched")) if t["matched_category"] == "Food"]
    assert row(client, "SWIGGY ORDER 62929")["tags"] == []
    assert sum("eating-out" in t["tags"] for t in food) == len(food) - 2  # both copies share the key


def test_group_rule_applies_and_clears_stale_exclusion(client, ledger):
    criteria = {"rule": "Netflix", "category": "Entertainment"}
    target = row(client, "NETFLIX")
    ok(client.post("/api/tags/transaction", json={"txn_id": target["anno_key"], "tags": ["subs"], "mode": "remove"}))
    ok(client.post("/api/tags/apply", json={"criteria": criteria, "tags": ["subs"]}))
    netflix = [t for t in ok(client.get("/api/transactions/enriched")) if "NETFLIX" in t["Description"]]
    assert all("subs" in t["tags"] for t in netflix)


def test_grouped_view_reports_rule_tags(client, ledger):
    ok(client.post("/api/tags/apply", json={"criteria": {"rule": "Amazon", "category": "Shopping"}, "tags": ["online"]}))
    groups = ok(client.get("/api/tags/grouped?columns=rule,category"))
    amazon = next(g for g in groups if g["criteria"] == {"rule": "Amazon", "category": "Shopping"})
    assert amazon["rule_tags"] == ["online"] and amazon["tag_counts"]["online"] == amazon["count"]


def test_rename_merge_and_delete(client, ledger):
    a = row(client, "SWIGGY ORDER 62929")["anno_key"]
    b = row(client, "NETFLIX")["anno_key"]
    ok(client.post("/api/transactions/annotate", json={"anno_key": a, "add_tags": ["food"]}))
    ok(client.post("/api/transactions/annotate", json={"anno_key": b, "add_tags": ["fun"]}))

    assert client.post("/api/tags/rename", json={"old_name": "nope", "new_name": "x"}).status_code == 404
    assert client.post("/api/tags/rename", json={"old_name": "food", "new_name": "  "}).status_code == 400
    assert ok(client.post("/api/tags/rename", json={"old_name": "fun", "new_name": "food"}))["merged"] is True
    vocab = {t["name"]: t for t in ok(client.get("/api/tags/vocabulary"))}
    assert list(vocab) == ["food"]

    ok(client.post("/api/tags/delete", json={"name": "food"}))
    assert ok(client.get("/api/tags/vocabulary")) == []


def test_bulk_annotate(client, ledger):
    keys = [t["anno_key"] for t in ok(client.get("/api/transactions/enriched")) if "RENT" in t["Description"]][:3]
    ok(client.post("/api/tags/bulk", json={"anno_keys": keys, "add_tags": ["rent"], "remarks": "flat"}))
    tagged = [t for t in ok(client.get("/api/transactions/enriched")) if t["anno_key"] in keys]
    assert all(t["tags"] == ["rent"] and t["manual_remarks"] == "flat" for t in tagged)


# --- Self transfers --------------------------------------------------------

def test_self_transfer_lifecycle(client, ledger):
    before = ok(client.get("/api/dashboard/summary"))["metrics"]
    pairs = ok(client.get("/api/self-transfers/search"))
    assert len(pairs) == 1 and pairs[0]["amount"] == 50000
    pair = pairs[0]
    body = [{"pair_id": pair["pair_id"], "debit_key": pair["debit_key"], "credit_key": pair["credit_key"],
             "amount": pair["amount"], "debit_date": pair["debit_txn"]["Date"],
             "credit_date": pair["credit_txn"]["Date"], "from_bank": "HDFC", "to_bank": "ICICI"}]
    assert ok(client.post("/api/self-transfers/save", json=body))["added"] == 1
    assert ok(client.post("/api/self-transfers/save", json=body))["added"] == 0  # idempotent

    summary = ok(client.get("/api/dashboard/summary"))
    assert abs(before["total_credit"] - summary["metrics"]["total_credit"] - 50000) < 0.01
    assert abs(before["total_debit"] - summary["metrics"]["total_debit"] - 50000) < 0.01
    assert abs(before["net_flow"] - summary["metrics"]["net_flow"]) < 0.01
    assert summary["transfers"]["total_moved"] == 50000
    assert ok(client.get("/api/self-transfers/search")) == []  # confirmed pairs aren't re-offered

    ok(client.post("/api/self-transfers/unmark", json={"pair_id": pair["pair_id"]}))
    assert ok(client.get("/api/dashboard/summary"))["metrics"]["total_debit"] == before["total_debit"]


def test_transfer_meta_defaults_to_end_of_ledger(client, ledger):
    meta = ok(client.get("/api/self-transfers/meta"))
    assert meta["end_date"] == "2024-12-22"


# --- Orphans and migration -------------------------------------------------

def test_annotation_survives_reparse_and_orphan_can_be_relinked(client, ledger, sandbox):
    target = row(client, "AMAZON RETAIL PURCHASE")
    ok(client.post("/api/transactions/annotate", json={"anno_key": target["anno_key"], "add_tags": ["gift"], "remarks": "for mom"}))

    # Re-parse changes the description beyond the key's tolerance.
    df = pd.read_csv(sandbox.MEGA_STATEMENT_CSV)
    df.loc[df["Description"] == "AMAZON RETAIL PURCHASE", "Description"] = "AMZN MKTP IN RETAIL"
    df.to_csv(sandbox.MEGA_STATEMENT_CSV, index=False)
    from api import enrichment
    enrichment.invalidate_enriched_cache()

    orphans = ok(client.get("/api/annotations/orphans"))
    assert len(orphans) == 1 and orphans[0]["tags"] == ["gift"]
    candidate = orphans[0]["candidates"][0]
    assert "AMZN" in candidate["description"]

    ok(client.post("/api/annotations/relink", json={"orphan_key": orphans[0]["orphan_key"], "target_key": candidate["anno_key"]}))
    assert ok(client.get("/api/annotations/orphans")) == []
    relinked = row(client, "AMZN MKTP")
    assert relinked["tags"] == ["gift"] and relinked["manual_remarks"] == "for mom"


def test_legacy_tags_and_remarks_are_migrated(sandbox, ledger, client):
    df = pd.read_csv(sandbox.MEGA_STATEMENT_CSV)
    txn_id = df.loc[df["Description"] == "AMAZON RETAIL PURCHASE", "TxnID"].iloc[0]
    with open(sandbox.TAGS_FILE, "w") as fh:
        json.dump({"transaction_tags": {txn_id: {"tags": ["old-tag"]}}}, fh)
    with open(sandbox.REMARKS_FILE, "w") as fh:
        json.dump({txn_id: "old remark"}, fh)

    migrated = row(client, "AMAZON RETAIL PURCHASE")
    assert migrated["tags"] == ["old-tag"] and migrated["manual_remarks"] == "old remark"


def test_saves_are_atomic_with_backup(client, ledger, sandbox):
    key = row(client, "NETFLIX")["anno_key"]
    ok(client.post("/api/transactions/annotate", json={"anno_key": key, "add_tags": ["a"]}))
    ok(client.post("/api/transactions/annotate", json={"anno_key": key, "add_tags": ["b"]}))
    import os
    assert os.path.exists(sandbox.ANNOTATIONS_FILE + ".bak")


def test_annotations_record_a_snapshot_of_their_transaction(client, ledger, sandbox):
    """BUG: build_context() was never called, so orphans had no date/amount to
    match on and the re-link dialog could never suggest a candidate."""
    target = row(client, "AMAZON RETAIL PURCHASE")
    ok(client.post("/api/transactions/annotate", json={"anno_key": target["anno_key"], "add_tags": ["gift"]}))
    ok(client.get("/api/transactions/enriched"))  # enrichment pass records the snapshot
    with open(sandbox.ANNOTATIONS_FILE) as fh:
        context = json.load(fh)["transactions"][target["anno_key"]]["context"]
    assert context["date"] == "2024-03-15" and context["debit"] == 2299 and context["bank"] == "HDFC"


def test_category_change_is_visible_without_restart(client, ledger):
    """BUG: the enriched-frame cache only watched the ledger and rules.json."""
    assert row(client, "SWIGGY ORDER 62929")["matched_category"] == "Food"
    ok(client.delete("/api/categories/cat-food"))
    assert row(client, "SWIGGY ORDER 62929")["matched_category"] == "Uncategorized"
