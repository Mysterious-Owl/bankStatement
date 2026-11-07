"""Recurring payments, trends, uncategorized coverage and budgets."""
import pytest


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def confirm_transfer(client):
    pair = ok(client.get("/api/self-transfers/search?start_date=2024-05-01&end_date=2024-05-31"))[0]
    ok(client.post("/api/self-transfers/save", json=[{
        "pair_id": pair["pair_id"], "debit_key": pair["debit_key"], "credit_key": pair["credit_key"],
        "amount": pair["amount"], "debit_date": pair["debit_txn"]["Date"], "credit_date": pair["credit_txn"]["Date"],
        "from_bank": pair["debit_txn"]["Bank"], "to_bank": pair["credit_txn"]["Bank"]}]))


# --- Recurring -------------------------------------------------------------

@pytest.fixture
def recurring(client, ledger):
    data = ok(client.get("/api/insights/recurring"))
    return data, {(i["payee"], i["direction"]): i for i in data["items"]}


def test_subscription_detected_with_price_change(recurring):
    _, items = recurring
    netflix = items[("NETFLIX SUBSCRIPTION", "out")]
    assert netflix["cadence"] == "monthly" and netflix["status"] == "active"
    assert any(c["from"] == 649 and c["to"] == 799 and c["date"].startswith("2024-07")
               for c in netflix["price_changes"])
    assert netflix["monthly_equivalent"] == 799  # current price, not the average


def test_stopped_and_income_items(recurring):
    _, items = recurring
    assert items[("GYM MEMBERSHIP FITZONE", "out")]["status"] == "stopped"
    salary = items[("SALARY CREDIT ACME CORP", "in")]
    assert salary["cadence"] == "monthly" and salary["monthly_equivalent"] == 120000


def test_irregular_spending_is_not_recurring(recurring):
    _, items = recurring
    assert not any(payee.startswith("SWIGGY") for payee, _ in items)


def test_committed_monthly_outflow(recurring):
    data, _ = recurring
    assert data["committed_monthly_out"] == 25000 + 799


# --- Trends ----------------------------------------------------------------

def test_trends_compare_on_last_complete_month(client, ledger):
    trends = ok(client.get("/api/insights/trends?months=12"))
    assert trends["months"][0] == "2024-01" and trends["months"][-1] == "2024-12"
    assert trends["latest_partial"] is True and trends["reference_month"] == "2024-11"
    entertainment = next(c for c in trends["categories"] if c["name"] == "Entertainment")
    assert entertainment["series"][5] == 649 and entertainment["series"][6] == 799


def test_confirmed_self_transfer_leaves_spending(client, ledger):
    may_before = ok(client.get("/api/insights/trends?months=12"))["debit"][4]
    confirm_transfer(client)
    may_after = ok(client.get("/api/insights/trends?months=12"))["debit"][4]
    assert abs((may_before - may_after) - 50000) < 0.01


@pytest.mark.parametrize("months", [1, 61])
def test_trend_window_is_validated(client, ledger, months):
    assert client.get(f"/api/insights/trends?months={months}").status_code == 400


# --- Uncategorized coverage ------------------------------------------------

def test_coverage_is_in_rupees_and_matches_ledger(client, ledger):
    coverage = ok(client.get("/api/insights/coverage"))
    rows = [t for t in ok(client.get("/api/transactions/enriched"))
            if not t["is_ignored"] and not t["is_self_transfer"] and t["Debit"] > 0]
    assert abs(coverage["spend_total"] - sum(t["Debit"] for t in rows)) < 0.01
    assert abs(coverage["uncategorized_total"]
               - sum(t["Debit"] for t in rows if t["matched_category"] == "Uncategorized")) < 0.01
    assert coverage["pct_spend"] > 3 * coverage["pct_rows"]
    top = coverage["top_groups"][0]
    assert "BUILDERS" in top["stem"] and top["suggested_pattern"] == "BUILDERS"


# --- Budgets ---------------------------------------------------------------

def test_budget_validation(client, ledger):
    ok(client.post("/api/budgets", json={"scope": "category", "target": "Food", "amount": 1500}))
    assert client.post("/api/budgets", json={"scope": "category", "target": "Food", "amount": 9}).status_code == 409
    assert client.post("/api/budgets", json={"scope": "category", "target": "X", "amount": -5}).status_code == 400
    assert client.post("/api/budgets", json={"scope": "nope", "target": "X", "amount": 5}).status_code == 400
    assert client.post("/api/budgets", json={"scope": "category", "target": " ", "amount": 5}).status_code == 400
    assert client.get("/api/budgets/status?month=2024-13").status_code == 400


def test_budget_status_rollover_pace_and_tag_scope(client, ledger):
    ok(client.post("/api/budgets", json={"scope": "category", "target": "Food", "amount": 1500,
                                         "rollover": True, "start_month": "2024-10"}))
    ok(client.post("/api/tags/apply", json={"criteria": {"rule": "Netflix", "category": "Entertainment"},
                                            "tags": ["subscriptions"]}))
    ok(client.post("/api/budgets", json={"scope": "tag", "target": "subscriptions", "amount": 700}))
    ok(client.post("/api/budgets", json={"scope": "category", "target": "Housing", "amount": 25000}))

    status = ok(client.get("/api/budgets/status?month=2024-12"))
    budgets = {b["target"]: b for b in status["budgets"]}

    food = [t for t in ok(client.get("/api/transactions/enriched"))
            if not t["is_ignored"] and t["matched_category"] == "Food"]
    spent = lambda month: sum(t["Debit"] for t in food if t["Date"].startswith(month))
    assert abs(budgets["Food"]["carry"] - ((1500 - spent("2024-10")) + (1500 - spent("2024-11")))) < 0.01
    assert abs(budgets["Food"]["actual"] - spent("2024-12")) < 0.01

    assert budgets["subscriptions"]["actual"] == 799 and budgets["subscriptions"]["status"] == "over"
    # Rent is paid on the 3rd; it must not be projected to happen again.
    assert budgets["Housing"]["projected"] == 25000 and budgets["Housing"]["status"] == "ok"
    assert 0 < status["pct_elapsed"] < 100
    assert status["coverage"]["spend_total"] > 0


def test_budget_suggestion_is_median_of_previous_months(client, ledger):
    suggestions = ok(client.get("/api/budgets/suggestions?month=2024-12"))
    housing = next(c for c in suggestions["categories"] if c["target"] == "Housing")
    assert housing["suggested"] == 25000


def test_budget_update_and_delete_are_logged(client, ledger):
    budget = ok(client.post("/api/budgets", json={"scope": "category", "target": "Food", "amount": 1500}))
    ok(client.post("/api/budgets", json={**budget, "amount": 2000}))
    assert ok(client.get("/api/budgets"))[0]["amount"] == 2000
    ok(client.delete(f"/api/budgets/{budget['id']}"))
    assert client.delete(f"/api/budgets/{budget['id']}").status_code == 404
    actions = [e["action"] for e in ok(client.get("/api/audit/events"))["events"]]
    assert {"budget_create", "budget_update", "budget_delete"} <= set(actions)


# --- Empty installs --------------------------------------------------------

@pytest.mark.parametrize("url", [
    "/api/insights/trends", "/api/insights/recurring", "/api/insights/coverage",
    "/api/budgets/status", "/api/budgets/suggestions", "/api/audit/balance",
    "/api/audit/duplicates", "/api/audit/review-progress", "/api/dashboard/summary",
    "/api/transactions/enriched", "/api/tags/grouped", "/api/self-transfers/search",
    "/api/self-transfers/meta", "/api/annotations/orphans", "/api/tags/vocabulary",
])
def test_every_endpoint_survives_a_fresh_install(client, url):
    """No statements uploaded yet: nothing may crash."""
    assert client.get(url).status_code == 200
