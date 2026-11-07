"""Statement parsing, the pipeline, uploads and reconciliation."""
import csv
import io
import json
import os

import pandas as pd
import pytest

import pipeline
import reconcile
from lib import parser


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


STANDARD_MAP = {"Date": "Date", "Narration": "Description", "Debit": "Debit",
                "Credit": "Credit", "Balance": "Balance"}
HEADER = list(STANDARD_MAP)


def write_statement(config, name, rows, header=HEADER, mapping=STANDARD_MAP, bank="HDFC"):
    """A raw CSV statement plus its column mapping."""
    with open(os.path.join(config.RAW_DIR, name), "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows([header] + rows)
    new = pd.DataFrame([{"FileName": name, "Bank": bank, "RawColumn": raw, "MappedTo": std}
                        for raw, std in mapping.items()])
    if os.path.exists(config.MAPPING_FILE):
        new = pd.concat([pd.read_csv(config.MAPPING_FILE), new], ignore_index=True)
    new.to_csv(config.MAPPING_FILE, index=False)


def run(config):
    summary = pipeline.run_pipeline()
    assert "error" not in summary, summary
    return pd.read_csv(config.MEGA_STATEMENT_CSV), summary


# --- Cleaning helpers ------------------------------------------------------

@pytest.mark.parametrize("value,is_balance,expected", [
    ("1,234.50", False, 1234.50),
    ("1,234.50 Cr", True, 1234.50),
    ("800.00 Dr", True, -800.0),
    ("800.00 Dr", False, 800.0),    # "Dr" on a debit column is a label, not a sign
    ("", False, 0.0),
    (None, False, 0.0),
    ("n/a", False, 0.0),
    (125, False, 125.0),
])
def test_clean_currency(value, is_balance, expected):
    assert pipeline.clean_currency(value, is_balance) == expected


def test_clean_description_collapses_whitespace():
    assert pipeline.clean_description(pd.Series(["  UPI/a\t b ", None, "c  d"])) == "UPI/a b c d"


def test_is_descending_by_balance_math():
    newest_first = pd.DataFrame({"Date": pd.to_datetime(["2024-01-02", "2024-01-01"]),
                                 "Debit": [100.0, 0.0], "Credit": [0.0, 500.0], "Balance": [400.0, 500.0]})
    assert pipeline.is_descending(newest_first)
    assert not pipeline.is_descending(newest_first.iloc[::-1].reset_index(drop=True))


def test_resolve_bank_priority():
    registry = {"HDFC": {}, "SBI": {}}
    assert pipeline.resolve_bank("x.csv", {"x.csv": "KOTAK"}, registry, {}) == "KOTAK"
    assert pipeline.resolve_bank("HDFC_fy25.xls", None, registry, {}) == "HDFC"
    assert pipeline.resolve_bank("mystery.csv", None, registry, {"mystery.csv": "Coop"}) == "Coop"
    assert pipeline.resolve_bank("mystery.csv", None, registry, {}) == "Unknown"


# --- Parser ----------------------------------------------------------------

def table_from(rows):
    df = pd.DataFrame(rows)
    table, _ = parser.find_table_start(df)
    return parser.remove_junk_rows(parser.find_table_end(table))


def test_header_detected_below_preamble():
    rows = [["Account statement", None, None], ["Name: X", None, None],
            ["Date", "Narration", "Debit"], ["01/01/2024", "A", "1"], ["02/01/2024", "B", "2"]]
    table = table_from(rows)
    assert list(table.columns) == ["Date", "Narration", "Debit"] and len(table) == 2


def test_footer_total_row_ends_the_table():
    rows = [["Date", "Narration", "Debit"], ["01/01/2024", "A", "1"],
            ["Total", None, "1"], ["Some disclaimer", "x", "y"]]
    assert len(table_from(rows)) == 1


def test_narration_containing_total_does_not_truncate_statement():
    """BUG: footer detection matched 'total' anywhere in the first 3 columns."""
    rows = [["Date", "Narration", "Debit"],
            ["01/01/2024", "TOTALENERGIES PETROL PUMP", "1500"],
            ["02/01/2024", "UPI/subtotal@ybl/GROCERY", "200"],
            ["03/01/2024", "SWIGGY", "300"]]
    assert len(table_from(rows)) == 3


def test_blank_line_near_end_keeps_last_transaction():
    """BUG: a single blank line within the last two rows ended the table there."""
    rows = [["Date", "Narration", "Debit"], ["01/01/2024", "A", "1"],
            [None, None, None], ["02/01/2024", "B", "2"]]
    assert list(table_from(rows)["Narration"]) == ["A", "B"]


def test_three_blank_lines_end_the_table():
    rows = [["Date", "Narration", "Debit"], ["01/01/2024", "A", "1"],
            [None] * 3, [None] * 3, [None] * 3, ["junk footer", "x", "y"]]
    assert len(table_from(rows)) == 1


# --- Pipeline end to end ---------------------------------------------------

def test_pipeline_normalizes_and_dedups_overlaps(sandbox):
    write_statement(sandbox, "HDFC_a.csv", [
        ["01/01/2024", "SALARY", "", "50,000.00", "60,000.00"],
        ["05/01/2024", "RENT", "20,000.00", "", "40,000.00"]])
    write_statement(sandbox, "HDFC_b.csv", [   # newest first, overlaps on 05/01
        ["07/01/2024", "AMAZON", "500", "", "39,500"],
        ["05/01/2024", "RENT", "20,000.00", "", "40,000.00"]])
    ledger, summary = run(sandbox)
    assert summary["total_rows_before_dedup"] == 4 and summary["total_rows_after_dedup"] == 3
    assert list(ledger["Description"]) == ["SALARY", "RENT", "AMAZON"]
    assert os.path.exists(sandbox.DUPLICATES_REPORT_CSV)


def test_identical_transactions_in_one_file_are_not_deduplicated(sandbox):
    """BUG: with no running balance, two same-day identical purchases shared a
    TxnID and one was deleted as a 'duplicate' -- silent data loss."""
    write_statement(sandbox, "PAYTM_wallet.csv", [
        ["01/01/2024", "UPI/chai@ybl/TEA", "15", "", ""],
        ["01/01/2024", "UPI/chai@ybl/TEA", "15", "", ""],
        ["02/01/2024", "UPI/auto@ybl/RIDE", "80", "", ""]], bank="PAYTM")
    ledger, _ = run(sandbox)
    assert len(ledger) == 3


def test_overlapping_files_with_repeated_transactions_still_dedup(sandbox):
    rows = [["01/01/2024", "UPI/chai@ybl/TEA", "15", "", ""],
            ["01/01/2024", "UPI/chai@ybl/TEA", "15", "", ""]]
    write_statement(sandbox, "PAYTM_jan.csv", rows, bank="PAYTM")
    write_statement(sandbox, "PAYTM_jan_again.csv", rows, bank="PAYTM")
    ledger, _ = run(sandbox)
    assert len(ledger) == 2


def test_amount_and_type_statements(sandbox):
    header = ["Date", "Description", "Amount", "Type", "Balance"]
    mapping = {"Date": "Date", "Description": "Description", "Amount": "Amount",
               "Type": "TxnType", "Balance": "Balance"}
    write_statement(sandbox, "KOTAK_x.csv", [
        ["01/01/2024", "A", "1,000", "CR", "1000"],
        ["02/01/2024", "B", "200", "DR", "800"],
        ["03/01/2024", "C", "50", "Debit", "750"],
        ["04/01/2024", "D", "25", "D", "725"],      # single-letter types
        ["05/01/2024", "E", "75", "C", "800"],
    ], header=header, mapping=mapping, bank="KOTAK")
    ledger, _ = run(sandbox)
    got = {r.Description: (r.Debit, r.Credit) for r in ledger.itertuples()}
    assert got == {"A": (0, 1000), "B": (200, 0), "C": (50, 0), "D": (25, 0), "E": (0, 75)}


def test_unclassifiable_type_is_flagged_not_silently_zeroed(sandbox):
    header = ["Date", "Description", "Amount", "Type", "Balance"]
    mapping = {"Date": "Date", "Description": "Description", "Amount": "Amount",
               "Type": "TxnType", "Balance": "Balance"}
    write_statement(sandbox, "KOTAK_y.csv", [["01/01/2024", "A", "1,000", "Card", "1000"]],
                    header=header, mapping=mapping, bank="KOTAK")
    ledger, _ = run(sandbox)
    assert "Card" in str(ledger.iloc[0]["SystemRemarks"])


def test_secondary_date_mismatch_noted(sandbox):
    header = ["Date", "Narration", "Value Dt", "Debit", "Credit", "Balance"]
    mapping = {**STANDARD_MAP, "Value Dt": "Date"}
    write_statement(sandbox, "HDFC_v.csv", [["01/01/2024", "A", "03/01/2024", "1", "", "99"]],
                    header=header, mapping=mapping)
    ledger, _ = run(sandbox)
    assert ledger.iloc[0]["SystemRemarks"].startswith("Value Dt: 2024-01-03")


def test_pipeline_without_mapping_file(sandbox):
    assert "error" in pipeline.run_pipeline()


# --- Upload API ------------------------------------------------------------

def upload(client, files, banks):
    return client.post("/api/upload", data={"bank_names": json.dumps(banks)},
                       files=[("files", (name, io.BytesIO(body.encode()), "text/csv")) for name, body in files])


SAMPLE = "Date,Narration,Debit,Credit,Balance\n01/01/2024,SALARY,,500,500\n02/01/2024,TEA,10,,490\n"


def test_upload_then_process(client, sandbox):
    staged = ok(upload(client, [("hdfc_jan.csv", SAMPLE)], ["HDFC"]))
    group = next(iter(staged["candidate_mappings"].values()))
    assert group["mappings"] == {"Date": "Date", "Narration": "Description", "Debit": "Debit",
                                 "Credit": "Credit", "Balance": "Balance"}
    result = ok(client.post("/api/process-upload", json={
        "upload_id": staged["upload_id"],
        "mappings": {"hdfc_jan.csv": group["mappings"]},
        "banks": {"hdfc_jan.csv": "HDFC"}}))
    assert result["pipeline_summary"]["total_rows_after_dedup"] == 2
    assert os.path.exists(os.path.join(sandbox.RAW_DIR, "hdfc_jan.csv"))


def test_upload_rejects_mismatched_bank_list(client):
    assert upload(client, [("a.csv", SAMPLE)], ["HDFC", "SBI"]).status_code == 400


def test_upload_filename_cannot_escape_the_upload_folder(client, sandbox):
    """BUG: the client-supplied filename was joined onto the path unchecked."""
    staged = ok(upload(client, [("HDFC_../../../escaped.csv", SAMPLE)], ["HDFC"]))
    names = [n for g in staged["candidate_mappings"].values() for n in g["filenames"]]
    assert all("/" not in n and "\\" not in n and ".." not in n for n in names)
    assert not os.path.exists(os.path.join(os.path.dirname(sandbox.UPLOAD_DIR), "escaped.csv"))


def test_processing_an_upload_does_not_overwrite_an_existing_raw_file(client, sandbox):
    """BUG: a new upload with the same name silently replaced an older raw statement."""
    with open(os.path.join(sandbox.RAW_DIR, "hdfc_jan.csv"), "w") as fh:
        fh.write("previous statement")
    staged = ok(upload(client, [("hdfc_jan.csv", SAMPLE)], ["HDFC"]))
    group = next(iter(staged["candidate_mappings"].values()))
    name = group["filenames"][0]
    ok(client.post("/api/process-upload", json={
        "upload_id": staged["upload_id"], "mappings": {name: group["mappings"]}, "banks": {name: "HDFC"}}))
    with open(os.path.join(sandbox.RAW_DIR, "hdfc_jan.csv")) as fh:
        assert fh.read() == "previous statement"
    assert len(os.listdir(sandbox.RAW_DIR)) == 2


def test_unknown_upload_session(client):
    assert client.post("/api/process-upload", json={"upload_id": "nope", "mappings": {}, "banks": {}}).status_code == 404


# --- Reconciliation --------------------------------------------------------

def test_reconcile_gaps_quiet_periods_and_redundant_files(sandbox):
    write_statement(sandbox, "HDFC_q1.csv", [
        ["01/01/2024", "A", "", "1000", "1000"],
        ["20/03/2024", "B", "100", "", "900"]])      # Jan->Mar silence inside one file
    write_statement(sandbox, "HDFC_q3.csv", [          # April-June missing entirely
        ["01/07/2024", "C", "100", "", "800"],
        ["30/09/2024", "D", "100", "", "700"]])
    write_statement(sandbox, "HDFC_q3_copy.csv", [
        ["01/07/2024", "C", "100", "", "800"],
        ["30/09/2024", "D", "100", "", "700"]])
    run(sandbox)
    bank = reconcile.run_reconciliation()["bank_level"][0]
    assert [(g["start"], g["end"]) for g in bank["CoverageGaps"]] == [("2024-03-21", "2024-06-30")]
    q1 = next(f for f in bank["Files"] if f["filename"] == "HDFC_q1.csv")
    assert q1["quiet_periods"] and q1["quiet_periods"][0]["start"] == "2024-01-02"
    assert [r["filename"] for r in bank["RedundantFiles"]] == ["HDFC_q3_copy.csv"]
    json.dumps(bank)  # must be JSON-serializable (numpy ints broke this once)


def test_reconcile_aggregate_is_independent_of_same_day_order(sandbox):
    """BUG: the bank aggregate took the 'first' row after an unstable sort on
    date alone, so same-day rows could flip a consistent bank to inconsistent."""
    write_statement(sandbox, "HDFC_d.csv", [
        ["01/01/2024", "A", "", "1000", "1000"],
        ["01/01/2024", "B", "300", "", "700"],
        ["01/01/2024", "C", "", "50", "750"],
        ["02/01/2024", "D", "100", "", "650"]])
    run(sandbox)
    df = pd.read_csv(sandbox.MEGA_STATEMENT_CSV).sample(frac=1, random_state=3)
    assert reconcile.check_bank_aggregate(df, "HDFC")["Consistency"].startswith("Consistent")


@pytest.mark.parametrize("cells,is_footer", [
    (["Total", None, "1"], True),
    ([None, "Total", "1"], True),
    (["Grand Total:", "", ""], True),
    (["31/03/2024", "Closing Balance", "12,345"], True),     # dated footer rows exist
    (["31/03/2024", "CLOSING BALANCE AS ON", ""], True),
    (["03/01/2024", "Total", "2"], True),
    (["Page No - 2", "", ""], True),
    (["** End of Statement **", None, None], True),
    (["Statement Summary", "", ""], True),
    (["01/01/2024", "TOTALENERGIES PETROL", "1500"], False),
    (["02/01/2024", "UPI/subtotal@ybl", "2"], False),
    (["03/01/2024", "TOTAL MALL PURCHASE", "2"], False),
    (["01/01/2024", "A", "1"], False),
    (["Opening", "", ""], False),
])
def test_footer_detection(cells, is_footer):
    assert parser.is_footer_row(cells) == is_footer
