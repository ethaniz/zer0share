import pandas as pd

from zer0share.schema import ADJ_FACTOR_COLS, DAILY_BASIC_COLS, DAILY_COLS


def pro_bar(client, ts_code=None, *, start_date=None, end_date=None, trade_date=None,
            asset="E", freq="D", adj=None, ma=None, factors=None, adjfactor=False,
            fields=None, limit=None, offset=None, refresh=False):
    if asset != "E" or freq != "D":
        raise NotImplementedError("cached pro_bar supports asset='E', freq='D'")
    if adj not in (None, "qfq", "hfq"):
        raise ValueError("adj must be None, qfq, or hfq")
    if type(adjfactor) is not bool or type(refresh) is not bool:
        raise ValueError("adjfactor and refresh must be booleans")
    if adjfactor and adj is None:
        raise ValueError("adjfactor requires qfq or hfq adjustment")
    symbols = client._symbols(ts_code)
    if len(symbols) != 1:
        raise ValueError("pro_bar requires exactly one stock")
    start, end = client._range(trade_date, start_date, end_date)
    client._page(limit, offset)
    if ma is not None and not isinstance(ma, (list, tuple)):
        raise ValueError("ma must be a list of positive integers")
    windows = list(dict.fromkeys(ma or []))
    if any(type(window) is not int or window <= 0 for window in windows):
        raise ValueError("ma must contain positive integers")
    if factors is not None and not isinstance(factors, (list, tuple)):
        raise ValueError("factors must be a list containing tor and/or vr")
    requested_factors = list(dict.fromkeys(factors or []))
    if any(factor not in ("tor", "vr") for factor in requested_factors):
        raise ValueError("supported factors are tor and vr")
    factor_columns = [
        column for factor, column in (("tor", "turnover_rate"), ("vr", "volume_ratio"))
        if factor in requested_factors
    ]
    columns = [*DAILY_COLS, *factor_columns]
    if adjfactor:
        columns.append("adj_factor")
    for window in windows:
        columns.extend([f"ma{window}", f"ma_v_{window}"])
    selected = client._fields(fields, columns)
    symbol = symbols[0]
    with client.store.connect() as connection:
        records = client._fill_range(
            connection, "daily", symbol, DAILY_COLS, "trade_date", start, end,
            refresh, {"ts_code": symbol},
        )
        result = pd.DataFrame(records, columns=DAILY_COLS).sort_values("trade_date", ascending=False)
        if result.empty:
            return pd.DataFrame(columns=selected)
        if adj is not None:
            factor_records = client._fill_range(
                connection, "adj_factor", symbol, ADJ_FACTOR_COLS, "trade_date",
                start, end, refresh, {"ts_code": symbol},
            )
            adjustments = pd.DataFrame(factor_records, columns=ADJ_FACTOR_COLS)
            result = result.merge(
                adjustments, on=["ts_code", "trade_date"], how="left", validate="one_to_one",
            ).sort_values("trade_date", ascending=False)
            values = pd.to_numeric(result["adj_factor"], errors="raise")
            if values.isna().any() or (values <= 0).any():
                raise ValueError("missing or invalid adjustment factors; refresh the requested range")
            latest = adjustments.sort_values("trade_date", ascending=False)["adj_factor"].iloc[0]
            multiplier = values / float(latest) if adj == "qfq" else values
            for column in ("open", "close", "high", "low", "pre_close"):
                result[column] = (pd.to_numeric(result[column]) * multiplier).round(2)
            if not adjfactor:
                result = result.drop(columns="adj_factor")
        else:
            result["pre_close"] = result["close"].shift(-1)
        result["change"] = (result["close"] - result["pre_close"]).round(2)
        result["pct_chg"] = (result["change"] / result["pre_close"].replace(0, float("nan")) * 100).round(2)
        if factor_columns:
            basic_records = client._fill_range(
                connection, "daily_basic", symbol, DAILY_BASIC_COLS, "trade_date",
                start, end, refresh, {"ts_code": symbol},
            )
            basic = pd.DataFrame(basic_records, columns=DAILY_BASIC_COLS)
            result = result.merge(
                basic[["ts_code", "trade_date", *factor_columns]],
                on=["ts_code", "trade_date"], how="left", validate="one_to_one",
            )
        result = result.sort_values("trade_date")
        for window in windows:
            result[f"ma{window}"] = result["close"].rolling(window).mean().round(2)
            result[f"ma_v_{window}"] = result["vol"].rolling(window).mean().round(2)
    result = result.sort_values("trade_date", ascending=False)
    return result.iloc[offset or 0:][:limit].loc[:, selected].reset_index(drop=True)
