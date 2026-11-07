"""
Small DataFrame helpers shared by the analysis modules (audit, analytics) and
the HTTP layer.

Every total in the app has to agree on which rows count and how amounts are
read, so those rules live here once instead of being re-derived per module.
"""
import pandas as pd


def bool_column(df, name):
    """A boolean column, or all-False when the frame doesn't carry it."""
    if name in df.columns:
        return df[name].fillna(False).astype(bool)
    return pd.Series(False, index=df.index)


def amount(series, fill=0.0):
    """Numeric amounts; anything unparseable becomes `fill` (pass None to keep NaN)."""
    numbers = pd.to_numeric(series, errors='coerce')
    return numbers if fill is None else numbers.fillna(fill)


def with_dates(df):
    """A copy with Date parsed to datetime and undated rows dropped."""
    out = df.copy()
    out['Date'] = pd.to_datetime(out['Date'], errors='coerce')
    return out.dropna(subset=['Date'])


def with_amounts(df, balance=False):
    """with_dates() plus numeric Debit/Credit (and Balance, kept NaN-able)."""
    out = with_dates(df)
    out['Debit'] = amount(out['Debit'])
    out['Credit'] = amount(out['Credit'])
    if balance:
        out['Balance'] = amount(out['Balance'], fill=None)
    return out


def live_rows(df):
    """Everything except rows the user has marked to ignore (e.g. a duplicate copy)."""
    return df[~bool_column(df, 'is_ignored')]


def flow_rows(df):
    """
    Real income and expenditure: live rows minus confirmed self transfers.
    Money moved between the user's own accounts is neither.
    """
    live = live_rows(df)
    return live[~bool_column(live, 'is_self_transfer')]
