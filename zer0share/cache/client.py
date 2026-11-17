import datetime as dt
import json
import re
import time
from zoneinfo import ZoneInfo

import pandas as pd

from zer0share.cache.config import CacheConfig
from zer0share.cache.store import CacheStore
from zer0share.schema import (
    ADJ_FACTOR_COLS, BASIC_COLS, DAILY_BASIC_COLS, DAILY_COLS, TRADE_CAL_COLS,
    STK_LIMIT_COLS, STOCK_ST_COLS, SUSPEND_D_COLS,
)


DAILY_APIS = {
    "daily": DAILY_COLS,
    "adj_factor": ADJ_FACTOR_COLS,
    "daily_basic": DAILY_BASIC_COLS,
    "stk_limit": STK_LIMIT_COLS,
    "stock_st": STOCK_ST_COLS,
    "suspend_d": SUSPEND_D_COLS,
}
EVENT_KEYS = {
    "stock_st": ("type",),
    "suspend_d": ("suspend_type", "suspend_timing"),
}
PAGE_LIMITS = {"stock_st": 1000, "suspend_d": 5000}
STORED_APIS = (*DAILY_APIS, "stock_basic", "trade_cal")
SUPPORTED_APIS = (*STORED_APIS, "pro_bar")


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        raise ValueError("dates must use YYYYMMDD")
    try:
        return dt.datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        raise ValueError("dates must be valid YYYYMMDD dates") from None


def _text(value):
    return value.strftime("%Y%m%d")


def _missing(start, end, intervals):
    cursor = start
    for covered_start, covered_end in intervals:
        covered_start, covered_end = _date(covered_start), _date(covered_end)
        if covered_end < cursor:
            continue
        if covered_start > end:
            break
        if covered_start > cursor:
            yield cursor, covered_start - dt.timedelta(days=1)
        if covered_end >= end:
            return
        cursor = max(cursor, covered_end + dt.timedelta(days=1))
    if cursor <= end:
        yield cursor, end


class CachedPro:
    def __init__(self, config: CacheConfig | None = None, *, upstream=None, clock=None, sleeper=None):
        self.config = config or CacheConfig()
        self.store = CacheStore(self.config.path, self.config.lock_timeout_seconds)
        self._upstream = upstream
        self._clock = clock or (lambda: dt.datetime.now(ZoneInfo("Asia/Shanghai")))
        self._sleep = sleeper or time.sleep

    def _now(self):
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("cache clock must return a timezone-aware datetime")
        return now.astimezone(ZoneInfo("Asia/Shanghai"))

    def _symbols(self, value):
        if isinstance(value, str):
            symbols = value.split(",")
        elif isinstance(value, (list, tuple)):
            symbols = list(value)
        else:
            raise ValueError("ts_code is required; market-wide requests are disabled")
        if any(not isinstance(symbol, str) for symbol in symbols):
            raise ValueError("ts_code must contain stock codes")
        symbols = list(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        if not symbols or any(not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) for symbol in symbols):
            raise ValueError("ts_code must contain specific A-share codes, e.g. 600519.SH")
        if len(symbols) > self.config.max_symbols:
            raise ValueError(f"at most {self.config.max_symbols} stocks per request")
        return symbols

    @staticmethod
    def _fields(fields, columns):
        if fields is None or fields == "":
            return list(columns)
        requested = fields.split(",") if isinstance(fields, str) else list(fields)
        if any(not isinstance(field, str) for field in requested):
            raise ValueError("fields must contain column names")
        requested = list(dict.fromkeys(field.strip() for field in requested))
        if not requested or set(requested) - set(columns):
            raise ValueError("unknown or empty fields")
        return requested

    @staticmethod
    def _page(limit, offset):
        for value in (limit, offset):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("limit and offset must be nonnegative integers")

    def _range(self, trade_date, start_date, end_date, *, calendar=False):
        if trade_date is not None:
            if start_date is not None or end_date is not None:
                raise ValueError("trade_date cannot be combined with a date range")
            start = end = _date(trade_date)
        else:
            if start_date is None:
                raise ValueError("start_date or trade_date is required; implicit full history is disabled")
            start = _date(start_date)
            end = _date(end_date) if end_date is not None else self._now().date()
        if end < start:
            raise ValueError("end_date must be on or after start_date")
        if not calendar and end > self._now().date():
            raise ValueError("future stock dates are not supported")
        return start, end

    def _fetch(self, connection, api, columns, **params):
        if self._upstream is None:
            if not self.config.token or self.config.token == "your_tushare_token_here":
                raise ValueError("a Tushare token is required on cache miss")
            import tushare

            self._upstream = tushare.pro_api(self.config.token)
        for attempt in range(self.config.retries + 1):
            last = connection.execute(
                "SELECT value FROM cache_state WHERE name = 'last_request'"
            ).fetchone()
            now = self._now().timestamp()
            if last is not None:
                delay = self.config.request_interval_seconds - (now - last[0])
                if delay > 0:
                    self._sleep(delay)
            connection.execute("""
                INSERT OR REPLACE INTO cache_state VALUES ('last_request', ?)
            """, [self._now().timestamp()])
            self.store.count(connection, api, remote_calls=1)
            try:
                frame = getattr(self._upstream, api)(fields=",".join(columns), **params)
                if not isinstance(frame, pd.DataFrame):
                    raise ValueError("Tushare must return a DataFrame")
                return frame
            except Exception:
                self.store.count(connection, api, remote_errors=1)
                if attempt >= self.config.retries:
                    raise
                self._sleep(min(2 ** attempt, 8))

    @staticmethod
    def _validate(frame, columns, date_column=None, start=None, end=None, symbol=None, exchange=None,
                  key_columns=()):
        if frame.empty:
            return frame.reindex(columns=columns)
        missing = set(columns) - set(frame.columns)
        if missing or frame.columns.duplicated().any():
            raise ValueError(f"incomplete Tushare response; missing fields: {sorted(missing)}")
        frame = frame.loc[:, columns].copy()
        if symbol is not None and not frame["ts_code"].eq(symbol).all():
            raise ValueError("Tushare returned an unexpected stock")
        if exchange is not None and not frame["exchange"].eq(exchange).all():
            raise ValueError("Tushare returned an unexpected exchange")
        if date_column:
            dates = frame[date_column].map(_date)
            if not dates.between(start, end).all() or frame.duplicated([date_column, *key_columns]).any():
                raise ValueError("Tushare returned out-of-range or duplicate dates")
        elif len(frame) > 1:
            raise ValueError("Tushare returned duplicate stock snapshots")
        return frame

    def _fetch_complete(self, connection, api, columns, **params):
        if api not in PAGE_LIMITS:
            return self._fetch(connection, api, columns, **params)
        limit = PAGE_LIMITS[api]
        frames = []
        signatures = set()
        offset = 0
        while True:
            frame = self._fetch(connection, api, columns, limit=limit, offset=offset, **params)
            if len(frame) > limit:
                raise ValueError("Tushare ignored the requested page size")
            if not frame.empty:
                signature = frame.to_json(orient="records", double_precision=15)
                if signature in signatures:
                    raise ValueError("Tushare repeated a page; response is not complete")
                signatures.add(signature)
                frames.append(frame)
            if len(frame) < limit:
                break
            offset += len(frame)
            if offset >= 100000:
                raise ValueError("response exceeded the per-stock safety limit")
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)

    def _expires(self, api, end, empty):
        policy = self.config.policies.get(api)
        now = self._now()
        recent_start = now.date() - dt.timedelta(days=self.config.recent_days - 1)
        if end >= recent_start:
            ttl = (
                policy.recent_ttl_seconds
                if policy and policy.recent_ttl_seconds is not None
                else self.config.recent_ttl_seconds
            )
        else:
            ttl = policy.historical_ttl_seconds if policy else None
        if empty and policy and policy.empty_ttl_seconds is not None:
            ttl = policy.empty_ttl_seconds if ttl is None else min(ttl, policy.empty_ttl_seconds)
        return None if ttl is None else now.timestamp() + ttl

    def _fill_range(self, connection, api, scope, columns, date_column, start, end, refresh, params):
        now = self._now()
        intervals = [] if refresh else self.store.intervals(connection, api, scope, now.timestamp())
        missing = list(_missing(start, end, intervals))
        category = "hits" if not missing else "misses" if missing == [(start, end)] else "partial_hits"
        self.store.count(connection, api, lookups=1, **{category: 1})
        recent_start = now.date() - dt.timedelta(days=self.config.recent_days - 1)
        for missing_start, missing_end in missing:
            cursor = missing_start
            while cursor <= missing_end:
                chunk_end = min(missing_end, cursor + dt.timedelta(days=self.config.chunk_days - 1))
                if cursor < recent_start <= chunk_end:
                    chunk_end = recent_start - dt.timedelta(days=1)
                frame = self._fetch_complete(
                    connection, api, columns,
                    start_date=_text(cursor), end_date=_text(chunk_end), **params,
                )
                frame = self._validate(
                    frame, columns, date_column, cursor, chunk_end,
                    symbol=params.get("ts_code"), exchange=params.get("exchange"),
                    key_columns=EVENT_KEYS.get(api, ()),
                )
                expires = self._expires(api, chunk_end, frame.empty)
                self.store.write(
                    connection, api, scope, _text(cursor), _text(chunk_end),
                    frame, date_column, expires, key_columns=EVENT_KEYS.get(api, ()),
                )
                if chunk_end == missing_end:
                    break
                cursor = chunk_end + dt.timedelta(days=1)
        return self.store.read(connection, api, scope, _text(start), _text(end))

    def _daily(self, api, ts_code=None, trade_date=None, start_date=None, end_date=None,
               fields=None, limit=None, offset=None, refresh=False):
        symbols = self._symbols(ts_code)
        start, end = self._range(trade_date, start_date, end_date)
        columns = DAILY_APIS[api]
        selected = self._fields(fields, columns)
        self._page(limit, offset)
        if type(refresh) is not bool:
            raise ValueError("refresh must be a boolean")
        records = []
        with self.store.connect() as connection:
            for symbol in symbols:
                records.extend(self._fill_range(
                    connection, api, symbol, columns, "trade_date", start, end,
                    refresh, {"ts_code": symbol},
                ))
        result = pd.DataFrame(records, columns=columns)
        result = result.sort_values(["trade_date", "ts_code"], ascending=[False, True])
        return result.iloc[offset or 0:][:limit].loc[:, selected].reset_index(drop=True)

    def daily(self, **kwargs):
        return self._daily("daily", **kwargs)

    def adj_factor(self, **kwargs):
        return self._daily("adj_factor", **kwargs)

    def daily_basic(self, **kwargs):
        return self._daily("daily_basic", **kwargs)

    def stk_limit(self, **kwargs):
        return self._daily("stk_limit", **kwargs)

    def stock_st(self, **kwargs):
        return self._daily("stock_st", **kwargs)

    def suspend_d(self, ts_code=None, trade_date=None, start_date=None, end_date=None,
                  suspend_type=None, fields=None, limit=None, offset=None, refresh=False):
        if suspend_type not in (None, "S", "R"):
            raise ValueError("suspend_type must be S, R, or None")
        selected = self._fields(fields, SUSPEND_D_COLS)
        self._page(limit, offset)
        result = self._daily(
            "suspend_d", ts_code=ts_code, trade_date=trade_date, start_date=start_date,
            end_date=end_date, refresh=refresh,
        )
        if suspend_type is not None:
            result = result.loc[result["suspend_type"] == suspend_type]
        return result.iloc[offset or 0:][:limit].loc[:, selected].reset_index(drop=True)

    def pro_bar(self, ts_code=None, **kwargs):
        from zer0share.cache.bars import pro_bar

        return pro_bar(self, ts_code=ts_code, **kwargs)

    def stock_basic(self, ts_code=None, *, list_status="L", exchange=None, market=None,
                    is_hs=None, name=None, fields=None, limit=None, offset=None, refresh=False):
        symbols = self._symbols(ts_code)
        selected = self._fields(fields, BASIC_COLS)
        self._page(limit, offset)
        if type(refresh) is not bool:
            raise ValueError("refresh must be a boolean")
        filters = {
            key: value for key, value in {
                "list_status": list_status, "exchange": exchange,
                "market": market, "is_hs": is_hs, "name": name,
            }.items() if value is not None
        }
        if any(not isinstance(value, str) for value in filters.values()):
            raise ValueError("stock_basic filters must be strings")
        records = []
        with self.store.connect() as connection:
            for symbol in symbols:
                params = {"ts_code": symbol, **filters}
                scope = json.dumps(params, sort_keys=True)
                intervals = self.store.intervals(connection, "stock_basic", scope, self._now().timestamp())
                self.store.count(
                    connection, "stock_basic", lookups=1,
                    **{"misses" if refresh or not intervals else "hits": 1},
                )
                if refresh or not intervals:
                    frame = self._fetch(connection, "stock_basic", BASIC_COLS, **params)
                    frame = self._validate(frame, BASIC_COLS, symbol=symbol)
                    policy = self.config.policies.get("stock_basic")
                    ttl = (
                        policy.snapshot_ttl_seconds
                        if policy and policy.snapshot_ttl_seconds is not None
                        else self.config.snapshot_ttl_seconds
                    )
                    if frame.empty and policy and policy.empty_ttl_seconds is not None:
                        ttl = min(ttl, policy.empty_ttl_seconds)
                    self.store.write(
                        connection, "stock_basic", scope, "", "", frame, None,
                        self._now().timestamp() + ttl,
                    )
                records.extend(self.store.read(connection, "stock_basic", scope, "", ""))
        result = pd.DataFrame(records, columns=BASIC_COLS).sort_values("ts_code")
        return result.iloc[offset or 0:][:limit].loc[:, selected].reset_index(drop=True)

    def trade_cal(self, exchange="SSE", start_date=None, end_date=None,
                  is_open=None, fields=None, limit=None, offset=None, refresh=False):
        if exchange not in {"SSE", "SZSE", "CFFEX", "SHFE", "DCE", "CZCE", "INE", "GFEX"}:
            raise ValueError("a specific supported exchange is required")
        start, end = self._range(None, start_date, end_date, calendar=True)
        selected = self._fields(fields, TRADE_CAL_COLS)
        self._page(limit, offset)
        if type(refresh) is not bool:
            raise ValueError("refresh must be a boolean")
        if is_open is not None and str(is_open) not in {"0", "1"}:
            raise ValueError("is_open must be 0 or 1")
        with self.store.connect() as connection:
            records = self._fill_range(
                connection, "trade_cal", exchange, TRADE_CAL_COLS, "cal_date",
                start, end, refresh, {"exchange": exchange},
            )
        result = pd.DataFrame(records, columns=TRADE_CAL_COLS).sort_values("cal_date", ascending=False)
        if is_open is not None:
            result = result.loc[result["is_open"].astype(str) == str(is_open)]
        return result.iloc[offset or 0:][:limit].loc[:, selected].reset_index(drop=True)

    def query(self, api_name, **kwargs):
        if api_name not in SUPPORTED_APIS:
            raise NotImplementedError(f"cache API not supported: {api_name}; supported: {', '.join(SUPPORTED_APIS)}")
        return getattr(self, api_name)(**kwargs)

    def clear_cache(self, api=None, *, ts_code=None, start_date=None, end_date=None):
        if api is not None and api not in STORED_APIS:
            raise ValueError("clear an underlying stored API, not a derived API")
        if ts_code is not None:
            symbols = self._symbols(ts_code)
            if len(symbols) != 1:
                raise ValueError("cache clearing requires one stock code")
            ts_code = symbols[0]
        if start_date is not None:
            _date(start_date)
        if end_date is not None:
            _date(end_date)
        if start_date and end_date and start_date > end_date:
            raise ValueError("end_date must be on or after start_date")
        if api == "stock_basic" and (start_date or end_date):
            raise ValueError("stock_basic is a snapshot; clear without dates")
        self.store.clear(api, ts_code=ts_code, start_date=start_date, end_date=end_date)

    def import_parquet(self, source, api, ts_code, start_date, end_date):
        from zer0share.cache.migration import import_parquet

        return import_parquet(self, source, api, ts_code, start_date, end_date)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise NotImplementedError(f"cache API not supported: {name}; supported: {', '.join(SUPPORTED_APIS)}")
