"""Dashboard aggregates and the enriched-data exports."""
import json
from typing import Optional

import pandas as pd
from fastapi import APIRouter
from fastapi.responses import Response

from api import helpers
from api.enrichment import get_enriched_data_df

router = APIRouter()

TOP_TRANSFER_ROUTES = 8
TOP_TAGS = 10


def _transfer_summary(transfers_df, include_transfers):
    """Headline numbers for money the user moved between their own accounts."""
    routes = []
    outbound = pd.DataFrame()
    if not transfers_df.empty and 'transfer_counterparty' in transfers_df.columns:
        outbound = transfers_df[transfers_df['transfer_direction'] == 'out']
        if not outbound.empty:
            grouped = (outbound.groupby(['Bank', 'transfer_counterparty'])['Debit']
                       .agg(['sum', 'count'])
                       .sort_values('sum', ascending=False)
                       .head(TOP_TRANSFER_ROUTES))
            routes = [
                {"from": str(idx[0]), "to": str(idx[1]),
                 "amount": float(row['sum']), "count": int(row['count'])}
                for idx, row in grouped.iterrows()
            ]

    return {
        "total_moved": float(transfers_df['Debit'].sum()),
        "pair_count": int(len(outbound)),
        "txn_count": int(len(transfers_df)),
        "routes": routes,
        "included_in_totals": include_transfers,
    }


def _cash_availability(df):
    """
    Total balance across all banks per day.

    Each bank's closing balance is carried forward over days it had no
    transactions, otherwise a quiet account would read as zero.
    """
    if df.empty:
        return pd.Series(dtype=float)

    all_dates = pd.date_range(df['Date'].min(), df['Date'].max())
    matrix = pd.DataFrame(index=all_dates)
    for bank in df['Bank'].unique():
        daily_close = df[df['Bank'] == bank].groupby(df['Date'].dt.date)['Balance'].last()
        daily_close.index = pd.to_datetime(daily_close.index)
        matrix[bank] = daily_close.reindex(all_dates).ffill().fillna(0)
    return matrix.sum(axis=1)


def _tag_counts(df):
    tags = [t for tag_list in df['tags'] if isinstance(tag_list, list) for t in tag_list]
    if not tags:
        return {}
    return pd.Series(tags).value_counts().head(TOP_TAGS).to_dict()


@router.get("/api/dashboard/summary")
async def get_dashboard_summary(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    banks: Optional[str] = None,
    include_transfers: bool = False,
):
    df = get_enriched_data_df()
    if df.empty:
        return {"error": "No data"}

    filtered_df = helpers.filter_rows(df, start_date, end_date, banks)
    # Rows marked "ignore" (typically the second copy of a duplicate) drop out
    # of every figure below. Balances are the exception: they are facts read
    # off the statement, so the cash chart keeps using the full frame.
    live_df = helpers.live_rows(filtered_df)

    # Money moved between the user's own accounts is not income or
    # expenditure. Counting it as both (as this endpoint used to) inflated
    # credit AND debit by the transferred amount, so it is split out here.
    transfer_mask = helpers.bool_column(live_df, 'is_self_transfer')
    transfers_df = live_df[transfer_mask]
    flow_df = live_df if include_transfers else live_df[~transfer_mask]

    total_credit = float(flow_df['Credit'].sum())
    total_debit = float(flow_df['Debit'].sum())

    daily = flow_df.groupby(flow_df['Date'].dt.date).agg({'Credit': 'sum', 'Debit': 'sum'}).sort_index()
    # Monthly is what the dashboard charts: daily bars over a year are noise.
    monthly = (flow_df.groupby(flow_df['Date'].dt.strftime('%Y-%m'))
               .agg({'Credit': 'sum', 'Debit': 'sum'}).sort_index())
    balances = _cash_availability(filtered_df)

    # Spending only; transfers are excluded alongside the headline metrics so a
    # category cannot be inflated by internal movement.
    categories = flow_df[flow_df['Debit'] > 0].groupby('matched_category')['Debit'].sum()
    categories = categories.sort_values(ascending=False)

    tag_counts = _tag_counts(flow_df)

    return {
        "metrics": {
            "total_credit": total_credit,
            "total_debit": total_debit,
            "net_flow": total_credit - total_debit,
            "txn_count": len(flow_df),
        },
        "transfers": _transfer_summary(transfers_df, include_transfers),
        "daily": {
            "labels": [d.strftime('%Y-%m-%d') for d in daily.index],
            "credit": daily['Credit'].tolist(),
            "debit": daily['Debit'].tolist(),
        },
        "monthly": {
            "labels": monthly.index.tolist(),
            "credit": [float(v) for v in monthly['Credit']],
            "debit": [float(v) for v in monthly['Debit']],
        },
        "cash_availability": {
            "labels": [d.strftime('%Y-%m-%d') for d in balances.index],
            "data": balances.tolist(),
        },
        "categories": {
            "labels": categories.index.tolist(),
            "values": categories.tolist(),
        },
        "tags": {
            "labels": list(tag_counts.keys()),
            "values": list(tag_counts.values()),
        },
        "banks": sorted(df['Bank'].unique().tolist()),
        # The full ledger's span, independent of the filters -- lets the page
        # default to a range that actually has data instead of today's FY.
        "date_range": {
            "min": df['Date'].min().strftime('%Y-%m-%d') if df['Date'].notna().any() else None,
            "max": df['Date'].max().strftime('%Y-%m-%d') if df['Date'].notna().any() else None,
        },
    }


@router.get("/api/transactions/enriched")
async def get_enriched_transactions():
    """
    Serialize the enriched frame as-is. Tags, remarks and self-transfer state
    are already resolved during enrichment -- this endpoint used to recompute
    tags with a context that passed the category *name* where an id was
    expected, which quietly overwrote the correct values with broken ones.
    """
    df = get_enriched_data_df()
    if df.empty:
        return []
    return helpers.records(df)


@router.get("/data.csv")
async def get_data_csv():
    """The enriched data as a downloadable CSV."""
    df = get_enriched_data_df()
    if df.empty:
        return ""

    export = df.copy()
    # Flatten the object columns; a Python repr in a CSV cell is unusable.
    if 'tags' in export.columns:
        export['tags'] = export['tags'].apply(
            lambda x: "; ".join(x) if isinstance(x, list) else "")
    if 'extracted_fields' in export.columns:
        export['extracted_fields'] = export['extracted_fields'].apply(
            lambda x: json.dumps(x) if isinstance(x, dict) else "")

    return Response(
        content=export.to_csv(index=False),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=mega_statement_enriched.csv"},
    )
