"""Balance continuity, near-duplicates, ignore/review state and the change log."""
import ledger as L


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def by_bank(results):
    return {b["bank"]: b for b in results}


# --- Balance continuity ----------------------------------------------------

def test_missing_statement_shows_as_one_seam_break_of_exact_amount(client, ledger):
    hdfc = by_bank(ok(client.get("/api/audit/balance")))["HDFC"]
    seams = [b for b in hdfc["breaks"] if b["kind"] == "seam"]
    assert len(seams) == 1
    assert abs(seams[0]["unexplained"] - ledger["missing_net"]) < 0.01
    assert (seams[0]["from_file"], seams[0]["to_file"]) == ("hdfc_q3.csv", "hdfc_q4.csv")


def test_same_day_rows_in_reverse_order_do_not_break_the_walk(client, ledger):
    hdfc = by_bank(ok(client.get("/api/audit/balance")))["HDFC"]
    assert not any(b["to_date"] == "2024-03-15" for b in hdfc["breaks"])


def test_bank_statuses(client, ledger):
    banks = by_bank(ok(client.get("/api/audit/balance")))
    assert banks["PAYTM"]["status"] == "no_balance"
    assert banks["ICICI"]["status"] == "continuous"
    assert banks["HDFC"]["status"] == "breaks"


def test_unignored_duplicate_is_a_break_on_its_day(client, ledger):
    hdfc = by_bank(ok(client.get("/api/audit/balance")))["HDFC"]
    day = [b for b in hdfc["breaks"] if b["to_date"] == L.DUP_DATE]
    assert len(day) == 1 and abs(day[0]["unexplained"] - 470) < 0.01


# --- Near duplicates -------------------------------------------------------

def _dup_group(client):
    groups = ok(client.get("/api/audit/duplicates"))
    assert len(groups) == 1
    return groups[0]


def test_planted_duplicate_found_with_high_confidence(client, ledger):
    g = _dup_group(client)
    assert g["confidence"] == "high"
    assert g["date"] == L.DUP_DATE and abs(g["amount"] + 470) < 0.01
    assert g["overstatement"] == 470


def test_identical_purchases_in_one_file_are_not_duplicates(client, ledger):
    assert not any(g["date"] == "2024-04-10" for g in ok(client.get("/api/audit/duplicates")))


def test_ignoring_one_copy_fixes_totals_balance_and_report(client, ledger):
    debit_before = ok(client.get("/api/dashboard/summary"))["metrics"]["total_debit"]
    copy = next(r for r in _dup_group(client)["rows"] if r["source_file"] == "hdfc_q3.csv")
    ok(client.post("/api/audit/ignore", json={"row_keys": [copy["row_key"]], "ignored": True}))

    debit_after = ok(client.get("/api/dashboard/summary"))["metrics"]["total_debit"]
    assert abs((debit_before - debit_after) - 470) < 0.01
    assert ok(client.get("/api/audit/duplicates")) == []
    hdfc = by_bank(ok(client.get("/api/audit/balance")))["HDFC"]
    assert not any(b["to_date"] == L.DUP_DATE for b in hdfc["breaks"])

    # Both copies share an anno_key; only the q3 one may be ignored.
    rows = [t for t in ok(client.get("/api/transactions/enriched")) if t["Date"].startswith(L.DUP_DATE)]
    assert sorted(t["is_ignored"] for t in rows) == [False, True]


def test_restoring_an_ignored_row(client, ledger):
    copy = next(r for r in _dup_group(client)["rows"] if r["source_file"] == "hdfc_q3.csv")
    ok(client.post("/api/audit/ignore", json={"row_keys": [copy["row_key"]], "ignored": True}))
    ok(client.post("/api/audit/ignore", json={"row_keys": [copy["row_key"]], "ignored": False}))
    assert len(ok(client.get("/api/audit/duplicates"))) == 1


def test_dismissed_group_stays_dismissed(client, ledger):
    g = _dup_group(client)
    ok(client.post("/api/audit/duplicates/dismiss", json={"group_id": g["group_id"]}))
    assert ok(client.get("/api/audit/duplicates")) == []


# --- Review progress and change log ---------------------------------------

def test_review_progress_is_measured_in_rupees(client, ledger):
    progress = ok(client.get("/api/audit/review-progress"))
    assert progress["rows_reviewed"] == 0
    biggest = progress["largest_unreviewed"][0]
    assert "BUILDERS" in biggest["description"]

    ok(client.post("/api/audit/review", json={"anno_keys": [biggest["anno_key"]], "reviewed": True}))
    after = ok(client.get("/api/audit/review-progress"))
    assert after["rows_reviewed"] == 1
    assert after["pct_spend"] > after["pct_rows"]


def test_lookalike_unreviewed_items_are_grouped(client, ledger):
    items = ok(client.get("/api/audit/review-progress"))["largest_unreviewed"]
    salary = next(g for g in items if "SALARY" in g["description"])
    assert salary["count"] == 11 and len(salary["keys"]) == 11

    ok(client.post("/api/audit/review", json={"anno_keys": salary["keys"], "reviewed": True}))
    after = ok(client.get("/api/audit/review-progress"))
    assert after["rows_reviewed"] == 11
    descriptions = [g["description"] for g in after["largest_unreviewed"]]
    assert len(descriptions) == len(set(descriptions))


def test_change_log_records_before_and_after(client, ledger):
    key = ok(client.get("/api/audit/review-progress"))["largest_unreviewed"][0]["anno_key"]
    ok(client.post("/api/audit/review", json={"anno_keys": [key], "reviewed": True}))
    ok(client.post("/api/transactions/annotate", json={"anno_key": key, "add_tags": ["property"], "remarks": "deposit"}))
    ok(client.post("/api/transactions/annotate", json={"anno_key": key, "remarks": "booking deposit"}))

    history = ok(client.get(f"/api/audit/events?key={key}"))["events"]
    assert [e["action"] for e in history] == ["annotate", "annotate", "review"]
    assert history[0]["changes"] == {"remarks": ["deposit", "booking deposit"]}


def test_event_limit_is_validated(client, ledger):
    assert client.get("/api/audit/events?limit=0").status_code == 400
    assert client.get("/api/audit/events?limit=501").status_code == 400
