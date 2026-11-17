import datetime as dt
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from zer0share.cache.client import DAILY_APIS, EVENT_KEYS, _date, _text


DIRECTORIES = {
    "daily": "daily_kline", "adj_factor": "adj_factor", "daily_basic": "daily_basic",
    "stk_limit": "stk_limit", "stock_st": "stock_st", "suspend_d": "suspend_d",
}


def import_parquet(client, source, api, ts_code, start_date, end_date):
    if api not in DIRECTORIES:
        raise ValueError("Parquet import supports the stored stock date APIs")
    symbols = client._symbols(ts_code)
    start, end = client._range(None, start_date, end_date)
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise ValueError("source must be an old Parquet data directory")
    table_dir = source / "stock" / DIRECTORIES[api]
    if not table_dir.is_dir():
        raise ValueError(f"missing old table directory: {table_dir}")
    report = dict(imported_rows=0, imported_dates=0, missing_partitions=0, empty_dates=0, existing_dates=0)
    prepared = []
    cursor = start
    columns = DAILY_APIS[api]
    while cursor <= end:
        date = _text(cursor)
        path = table_dir / f"date={date}" / "data.parquet"
        if not path.is_file():
            report["missing_partitions"] += 1
        else:
            frame = pq.read_table(path, schema=pq.read_schema(path)).to_pandas()
            if "ts_code" not in frame:
                raise ValueError(f"missing ts_code in {path}")
            for symbol in symbols:
                selected = frame.loc[frame["ts_code"] == symbol]
                if selected.empty:
                    report["empty_dates"] += 1
                    continue
                selected = client._validate(
                    selected, columns, "trade_date", cursor, cursor, symbol=symbol,
                    key_columns=EVENT_KEYS.get(api, ()),
                )
                prepared.append((symbol, date, selected))
        if cursor == end:
            break
        cursor += dt.timedelta(days=1)
    with client.store.connect() as connection:
        for symbol, date, frame in prepared:
            existing = connection.execute("""
                SELECT 1 FROM cache_coverage WHERE api = ? AND scope = ?
                AND start_date <= ? AND end_date >= ? LIMIT 1
            """, [api, symbol, date, date]).fetchone()
            if existing is not None:
                report["existing_dates"] += 1
                continue
            client.store.write(
                connection, api, symbol, date, date, frame, "trade_date",
                client._expires(api, _date(date), frame.empty),
                key_columns=EVENT_KEYS.get(api, ()),
            )
            report["imported_dates"] += 1
            report["imported_rows"] += len(frame)
    return report
