"""
Synthetic ledger with known answers, shared by the audit, insights, budget and
tagging tests. Written into whatever paths `config` currently names -- always
the pytest sandbox (see conftest.py), never the real data/ folder.

What's planted, and what each check should therefore find:

HDFC, a balance-consistent running stream across three files
  * hdfc_h1.csv Jan-Jun 2024, hdfc_q3.csv Jul-Sep, hdfc_q4.csv Oct-Dec
  * MISSING STATEMENT: every HDFC row 2024-09-25..2024-10-05 removed
      -> one "seam" balance break between hdfc_q3.csv and hdfc_q4.csv whose
         unexplained amount equals the net of the removed rows (missing_net)
  * 2024-03-15: three same-day rows in reverse file order -> no false break
  * SURVIVING DUPLICATE: the 2024-06-29 Swiggy order is in hdfc_h1 and hdfc_q3
      with different RefNumbers -> one high-confidence near-duplicate group
  * two genuine identical chai purchases on 2024-04-10 in ONE file -> not a dup
  * Netflix 649/month Jan-Jun, 799 from Jul      -> recurring + price change
  * Rent 25,000 on the 3rd                        -> recurring, active
  * Gym 1,500 on the 10th, Jan-May only           -> recurring, stopped
  * Salary 1,20,000 on the 1st                    -> recurring income
  * Swiggy on random days                         -> NOT recurring
  * 2,00,000 to "M S BUILDERS" (no rule matches)  -> dominates uncategorized
  * 50,000 HDFC -> ICICI self transfer on 2024-05-08/09
PAYTM: no running balance (all zero)              -> "no_balance"
ICICI: small consistent stream                    -> "continuous"
"""
import hashlib
import json
import random

import pandas as pd

MISSING_FROM, MISSING_TO = "2024-09-25", "2024-10-05"
DUP_DATE = "2024-06-29"


def _hdfc_events():
    rng = random.Random(7)
    ev = []
    for m in range(1, 13):
        d = lambda day: f"2024-{m:02d}-{day:02d}"
        ev.append((d(1), "SALARY CREDIT ACME CORP", 0, 120000))
        ev.append((d(3), "NEFT RENT TO LANDLORD SHARMA", 25000, 0))
        ev.append((d(5), "NETFLIX SUBSCRIPTION", 649 if m <= 6 else 799, 0))
        if m <= 5:
            ev.append((d(10), "GYM MEMBERSHIP FITZONE", 1500, 0))
        for _ in range(2):
            day = rng.choice([7, 12, 16, 19, 22, 26])
            ev.append((d(day), f"SWIGGY ORDER {rng.randint(10000, 99999)}", rng.choice([310, 420, 560, 640]), 0))
    ev.append(("2024-03-15", "SWIGGY ORDER 31501", 380, 0))
    ev.append(("2024-03-15", "UPI/chai@ybl/TEA", 15, 0))
    ev.append(("2024-03-15", "AMAZON RETAIL PURCHASE", 2299, 0))
    ev.append(("2024-04-10", "UPI/chai@ybl/TEA", 15, 0))
    ev.append(("2024-04-10", "UPI/chai@ybl/TEA", 15, 0))
    ev.append((DUP_DATE, "SWIGGY ORDER 62929", 470, 0))
    ev.append(("2024-05-08", "NEFT TRANSFER TO ICICI SELF", 50000, 0))
    ev.append(("2024-08-20", "NEFT TO M S BUILDERS PROPERTY", 200000, 0))
    ev.sort(key=lambda e: e[0])
    return ev


def _stream(bank, events, opening, file_of, owner="self"):
    rows, bal = [], opening
    for date, desc, dr, cr in events:
        bal = round(bal + cr - dr, 2)
        rows.append({"Date": date, "Description": desc, "Debit": float(dr), "Credit": float(cr),
                     "Balance": bal, "RefNumber": "", "Bank": bank, "Owner": owner,
                     "SourceFile": file_of(date)})
    return rows


def build(config):
    """Write the planted ledger and its config into the (sandboxed) config paths."""
    json.dump([
        {"id": "cat-income", "name": "Income", "color": "#34c759"},
        {"id": "cat-food", "name": "Food", "color": "#ff9f0a"},
        {"id": "cat-ent", "name": "Entertainment", "color": "#bf5af2"},
        {"id": "cat-home", "name": "Housing", "color": "#0a84ff"},
        {"id": "cat-health", "name": "Health", "color": "#ff375f"},
        {"id": "cat-shop", "name": "Shopping", "color": "#64d2ff"},
    ], open(config.CATEGORIES_FILE, "w"))
    json.dump([
        {"id": "r-sal", "name": "Salary", "pattern": "SALARY", "category_id": "cat-income", "priority": 10},
        {"id": "r-swg", "name": "Swiggy", "pattern": "SWIGGY", "category_id": "cat-food", "priority": 10},
        {"id": "r-nfx", "name": "Netflix", "pattern": "NETFLIX", "category_id": "cat-ent", "priority": 10},
        {"id": "r-rent", "name": "Rent", "pattern": "RENT", "category_id": "cat-home", "priority": 10},
        {"id": "r-gym", "name": "Gym", "pattern": "GYM", "category_id": "cat-health", "priority": 10},
        {"id": "r-amz", "name": "Amazon", "pattern": "AMAZON", "category_id": "cat-shop", "priority": 10},
    ], open(config.RULES_FILE, "w"))
    pd.DataFrame([{"Name": b, "Owner": "self", "Tags": ""} for b in ("HDFC", "ICICI", "PAYTM")]).to_csv(config.BANKS_FILE, index=False)

    def hdfc_file(date):
        if date <= "2024-06-30":
            return "hdfc_h1.csv"
        return "hdfc_q3.csv" if date <= "2024-09-30" else "hdfc_q4.csv"

    hdfc = _stream("HDFC", _hdfc_events(), 100000.0, hdfc_file)
    missing = [r for r in hdfc if MISSING_FROM <= r["Date"] <= MISSING_TO]
    missing_net = round(sum(r["Credit"] - r["Debit"] for r in missing), 2)
    hdfc = [r for r in hdfc if not (MISSING_FROM <= r["Date"] <= MISSING_TO)]

    # SortIndex per file, oldest first ...
    counters = {}
    for r in hdfc:
        counters[r["SourceFile"]] = counters.get(r["SourceFile"], -1) + 1
        r["SortIndex"] = counters[r["SourceFile"]]
    # ... except 2024-03-15, written newest-first as some exports do.
    block = [r for r in hdfc if r["Date"] == "2024-03-15"]
    idx = [r["SortIndex"] for r in block]
    for r, i in zip(block, reversed(idx)):
        r["SortIndex"] = i

    # Surviving duplicate: the same Swiggy order also present in the Q3 export,
    # with a reference number the H1 export didn't carry.
    original = next(r for r in hdfc if r["Date"] == DUP_DATE)
    copy = dict(original, SourceFile="hdfc_q3.csv", RefNumber="Q3REF0629", SortIndex=-1)
    hdfc.append(copy)

    icici = _stream("ICICI", [
        ("2024-02-02", "UPI/grocer@okicici/VEG", 900, 0),
        ("2024-05-09", "NEFT INWARD HDFC SELF", 0, 50000),
        ("2024-06-14", "AMAZON PRIME RENEWAL", 1499, 0),
        ("2024-11-21", "SWIGGY ORDER 77310", 520, 0),
    ], 20000.0, lambda d: "icici_2024.csv")
    for i, r in enumerate(icici):
        r["SortIndex"] = i

    paytm = _stream("PAYTM", [
        ("2024-03-03", "UPI/autowala@paytm/RIDE", 120, 0),
        ("2024-07-17", "UPI/kirana@paytm/MILK", 64, 0),
    ], 0.0, lambda d: "paytm_wallet.csv")
    for i, r in enumerate(paytm):
        r["Balance"] = 0.0
        r["SortIndex"] = i

    rows = hdfc + icici + paytm
    df = pd.DataFrame(rows)
    df["OriginalIndex"] = range(len(df))
    df["TxnType"] = ""
    df["Tags"] = ""
    df["SystemRemarks"] = ""
    df["TxnID"] = [hashlib.md5(f"{r.Date}{r.Description}{r.Debit}{r.Credit}{r.Balance}{r.RefNumber}{r.SourceFile}".encode()).hexdigest()
                   for r in df.itertuples()]
    df.to_csv(config.MEGA_STATEMENT_CSV, index=False)
    return {"config": config, "missing_net": missing_net, "rows": len(df)}
