from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import datetime as dt
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import duckdb

from zer0share import CachedPro, CacheConfig, pro_api
from zer0share.cache import load_cache_config
from zer0share.schema import ADJ_FACTOR_COLS, BASIC_COLS, DAILY_BASIC_COLS, DAILY_COLS, TRADE_CAL_COLS


NOW = dt.datetime(2026, 11, 17, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
SYMBOL = "600519.SH"
OTHER_SYMBOL = "601318.SH"
COLUMNS = {
    "daily": DAILY_COLS, "adj_factor": ADJ_FACTOR_COLS, "daily_basic": DAILY_BASIC_COLS,
    "stock_basic": BASIC_COLS, "trade_cal": TRADE_CAL_COLS,
}


class FakeTushare:
    def __init__(self):
        self.calls = []
        self.empty = False
        self.error = None
        self.drop_column = None
        self.wrong_symbol = False
        self.price = 10.0
        self.delay = 0

    def __getattr__(self, api):
        def fetch(**params):
            self.calls.append((api, dict(params)))
            time.sleep(self.delay)
            if self.error:
                raise self.error
            columns = COLUMNS[api]
            records = []
            if not self.empty:
                if api == "stock_basic":
                    record = dict.fromkeys(columns, None)
                    record.update(ts_code=params["ts_code"], list_status=params.get("list_status", "L"))
                    records.append(record)
                else:
                    for date in pd.date_range(params["start_date"], params["end_date"]):
                        record = dict.fromkeys(columns, self.price)
                        if api == "trade_cal":
                            record.update(
                                exchange=params["exchange"], cal_date=date.strftime("%Y%m%d"),
                                pretrade_date=(date - dt.timedelta(days=1)).strftime("%Y%m%d"),
                                is_open=int(date.weekday() < 5),
                            )
                        else:
                            record.update(
                                ts_code=OTHER_SYMBOL if self.wrong_symbol else params["ts_code"],
                                trade_date=date.strftime("%Y%m%d"),
                            )
                        records.append(record)
            frame = pd.DataFrame(records, columns=columns)
            if self.drop_column:
                frame = frame.drop(columns=self.drop_column)
            return frame
        return fetch


@pytest.fixture
def cache(tmp_path):
    remote = FakeTushare()
    config = CacheConfig(path=tmp_path / "cache.duckdb", request_interval_seconds=0, retries=0)
    clock = [NOW]
    client = CachedPro(config, upstream=remote, clock=lambda: clock[0])
    return client, remote, clock


def request(client, **kwargs):
    params = dict(ts_code=SYMBOL, start_date="20260105", end_date="20260109")
    params.update(kwargs)
    return client.daily(**params)


@pytest.mark.parametrize("api", ["daily", "adj_factor", "daily_basic"])
def test_repeat_and_fields_hit_without_remote_call(cache, api):
    client, remote, _ = cache
    full = client.query(api, ts_code=SYMBOL, start_date="20260105", end_date="20260109")
    selected = client.query(api, ts_code=SYMBOL, trade_date="20260106", fields="trade_date,ts_code")
    assert len(remote.calls) == 1
    assert len(full) == 5
    assert list(selected.columns) == ["trade_date", "ts_code"]
    assert selected["trade_date"].tolist() == ["20260106"]
    assert remote.calls[0][1]["ts_code"] == SYMBOL
    assert remote.calls[0][1]["fields"] == ",".join(COLUMNS[api])


def test_expanding_range_fetches_only_missing_edges(cache):
    client, remote, _ = cache
    request(client)
    result = request(client, start_date="20260101", end_date="20260112")
    assert [(params["start_date"], params["end_date"]) for _, params in remote.calls] == [
        ("20260105", "20260109"), ("20260101", "20260104"), ("20260110", "20260112"),
    ]
    assert len(result) == 12
    assert not result.duplicated(["ts_code", "trade_date"]).any()


def test_fills_middle_hole_and_merges_coverage(cache):
    client, remote, _ = cache
    request(client, end_date="20260106")
    request(client, start_date="20260108")
    request(client)
    assert remote.calls[-1][1]["start_date"] == "20260107"
    assert remote.calls[-1][1]["end_date"] == "20260107"
    request(client)
    assert len(remote.calls) == 3


def test_symbols_are_independent_normalized_and_deduplicated(cache):
    client, remote, _ = cache
    request(client)
    result = request(client, ts_code=[SYMBOL.lower(), OTHER_SYMBOL, SYMBOL])
    assert len(remote.calls) == 2
    assert set(result["ts_code"]) == {SYMBOL, OTHER_SYMBOL}
    assert len(result) == 10


def test_reopen_persists_cache_and_does_not_need_token(cache):
    client, remote, _ = cache
    expected = request(client)
    reopened = CachedPro(client.config, clock=lambda: NOW)
    pd.testing.assert_frame_equal(request(reopened), expected)
    assert len(remote.calls) == 1


def test_historical_empty_results_are_covered(cache):
    client, remote, _ = cache
    remote.empty = True
    assert request(client).empty
    assert request(client).empty
    assert len(remote.calls) == 1
    assert client.store.status().iloc[0]["scopes"] == 1


def test_recent_empty_results_expire_and_refetch(cache):
    client, remote, clock = cache
    remote.empty = True
    assert request(client, trade_date="20261117", start_date=None, end_date=None).empty
    remote.empty = False
    assert request(client, trade_date="20261117", start_date=None, end_date=None).empty
    clock[0] += dt.timedelta(seconds=301)
    assert len(request(client, trade_date="20261117", start_date=None, end_date=None)) == 1
    assert len(remote.calls) == 2


def test_recent_nonempty_rows_refresh_after_expiry(cache):
    client, remote, clock = cache
    request(client, start_date="20261116", end_date="20261117")
    remote.price = 20
    clock[0] += dt.timedelta(seconds=301)
    result = request(client, start_date="20261116", end_date="20261117")
    assert result["close"].tolist() == [20, 20]
    assert len(remote.calls) == 2


def test_old_and_recent_parts_have_independent_expiry(cache):
    client, remote, clock = cache
    request(client, start_date="20261101", end_date="20261117")
    assert [(params["start_date"], params["end_date"]) for _, params in remote.calls] == [
        ("20261101", "20261110"), ("20261111", "20261117"),
    ]
    clock[0] += dt.timedelta(seconds=301)
    request(client, start_date="20261101", end_date="20261117")
    assert len(remote.calls) == 3
    assert remote.calls[-1][1]["start_date"] == "20261111"


def test_refresh_replaces_rows_and_removes_stale_records(cache):
    client, remote, _ = cache
    request(client)
    remote.price = 99
    assert request(client, refresh=True)["close"].tolist() == [99] * 5
    remote.empty = True
    assert request(client, refresh=True).empty
    assert request(client).empty
    assert len(remote.calls) == 3


def test_failed_refresh_preserves_previous_cache(cache):
    client, remote, _ = cache
    expected = request(client)
    remote.error = RuntimeError("permission denied")
    with pytest.raises(RuntimeError, match="permission denied"):
        request(client, refresh=True)
    pd.testing.assert_frame_equal(request(client), expected)


@pytest.mark.parametrize("failure", ["error", "missing", "symbol"])
def test_failures_do_not_mark_ranges_as_covered(cache, failure):
    client, remote, _ = cache
    if failure == "error":
        remote.error = RuntimeError("network failed")
    elif failure == "missing":
        remote.drop_column = "close"
    else:
        remote.wrong_symbol = True
    with pytest.raises((ValueError, RuntimeError)):
        request(client)
    remote.error = None
    remote.drop_column = None
    remote.wrong_symbol = False
    assert len(request(client)) == 5
    assert len(remote.calls) == 2


def test_chunking_never_fetches_other_stocks(cache):
    client, remote, _ = cache
    client.config = replace(client.config, chunk_days=2)
    request(client)
    assert [(params["start_date"], params["end_date"]) for _, params in remote.calls] == [
        ("20260105", "20260106"), ("20260107", "20260108"), ("20260109", "20260109"),
    ]
    assert all(params["ts_code"] == SYMBOL for _, params in remote.calls)


@pytest.mark.parametrize("kwargs", [
    {"ts_code": None}, {"ts_code": ""}, {"ts_code": "invalid"},
    {"start_date": None}, {"start_date": "20260230"}, {"start_date": "2026-01-05"},
    {"end_date": "20260101"}, {"end_date": "20261118"},
    {"trade_date": "20260105"}, {"fields": "unknown"},
    {"limit": -1}, {"offset": True}, {"refresh": "yes"},
])
def test_invalid_requests_do_not_call_upstream(cache, kwargs):
    client, remote, _ = cache
    with pytest.raises(ValueError):
        request(client, **kwargs)
    assert remote.calls == []


def test_max_symbols_and_no_unbounded_history(cache):
    client, remote, _ = cache
    client.config = replace(client.config, max_symbols=1)
    with pytest.raises(ValueError, match="at most"):
        request(client, ts_code=f"{SYMBOL},{OTHER_SYMBOL}")
    with pytest.raises(ValueError, match="implicit full history"):
        client.daily(ts_code=SYMBOL)
    assert remote.calls == []


def test_pagination_is_local_and_never_poison_coverage(cache):
    client, remote, _ = cache
    result = request(client, limit=2, offset=1, fields="trade_date,close")
    assert result["trade_date"].tolist() == ["20260108", "20260107"]
    assert len(request(client)) == 5
    assert len(remote.calls) == 1
    assert "limit" not in remote.calls[0][1]
    assert request(client, limit=0).empty


def test_stock_basic_snapshot_fields_filters_and_expiry(cache):
    client, remote, clock = cache
    assert len(client.stock_basic(ts_code=SYMBOL)) == 1
    client.stock_basic(ts_code=SYMBOL, fields="name")
    assert len(remote.calls) == 1
    client.stock_basic(ts_code=SYMBOL, list_status="D")
    assert len(remote.calls) == 2
    clock[0] += dt.timedelta(days=2)
    client.stock_basic(ts_code=SYMBOL)
    assert len(remote.calls) == 3


def test_calendar_only_fetches_requested_exchange_and_filters_locally(cache):
    client, remote, _ = cache
    result = client.trade_cal(exchange="SZSE", start_date="20260105", end_date="20260111", is_open=1)
    assert len(result) == 5
    assert len(client.trade_cal(exchange="SZSE", start_date="20260105", end_date="20260111")) == 7
    assert len(remote.calls) == 1
    assert remote.calls[0][1]["exchange"] == "SZSE"
    assert "is_open" not in remote.calls[0][1]


def test_unsupported_api_fails_explicitly(cache):
    client, remote, _ = cache
    with pytest.raises(NotImplementedError):
        client.query("fund_daily", ts_code=SYMBOL)
    with pytest.raises(NotImplementedError):
        client.fund_daily(ts_code=SYMBOL)
    assert remote.calls == []


def test_thread_requests_share_one_fetch(cache):
    client, remote, _ = cache
    remote.delay = 0.1
    other = CachedPro(client.config, upstream=remote, clock=lambda: NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, [client, other]))
    assert [len(result) for result in results] == [5, 5]
    assert len(remote.calls) == 1


def test_process_requests_share_one_fetch(tmp_path):
    script = """
import datetime as dt
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo
import pandas as pd
from zer0share import CachedPro, CacheConfig
from zer0share.schema import ADJ_FACTOR_COLS
class Upstream:
    def adj_factor(self, **params):
        with Path(sys.argv[2]).open('a') as stream:
            stream.write('fetch\\n')
        time.sleep(0.2)
        return pd.DataFrame([['600519.SH', '20260105', 1.0]], columns=ADJ_FACTOR_COLS)
client = CachedPro(
    CacheConfig(path=Path(sys.argv[1]), retries=0, request_interval_seconds=0),
    upstream=Upstream(), clock=lambda: dt.datetime(2026, 11, 17, tzinfo=ZoneInfo('Asia/Shanghai')),
)
assert len(client.adj_factor(ts_code='600519.SH', trade_date='20260105')) == 1
"""
    arguments = [sys.executable, "-c", script, str(tmp_path / "shared.duckdb"), str(tmp_path / "calls")]
    processes = [subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stdout + stderr
    assert (tmp_path / "calls").read_text().splitlines() == ["fetch"]


def test_atomic_write_rolls_back_records_and_coverage(cache):
    client, remote, _ = cache
    expected = request(client)
    duplicate_rows = pd.concat([expected, expected], ignore_index=True)
    with client.store.connect() as connection:
        before = connection.execute("SELECT * FROM cache_coverage").fetchall()
        with pytest.raises(duckdb.ConstraintException):
            client.store.write(
                connection, "daily", SYMBOL, "20260105", "20260109",
                duplicate_rows, "trade_date", None,
            )
        assert connection.execute("SELECT * FROM cache_coverage").fetchall() == before
    pd.testing.assert_frame_equal(request(client), expected)


def test_clear_removes_coverage_and_is_scoped(cache):
    client, remote, _ = cache
    request(client)
    client.adj_factor(ts_code=SYMBOL, trade_date="20260105")
    client.store.clear("daily")
    client.adj_factor(ts_code=SYMBOL, trade_date="20260105")
    request(client)
    assert len(remote.calls) == 3
    client.store.clear()
    assert client.store.status().empty


def test_public_factory_and_configuration(tmp_path, monkeypatch):
    config_file = tmp_path / "settings.toml"
    cache_path = tmp_path / "cache.duckdb"
    config_file.write_text(f'[tushare]\ntoken = "test"\n[cache]\npath = "{cache_path}"\n')
    client = pro_api(config_file, upstream=FakeTushare())
    assert isinstance(client, CachedPro)
    assert len(request(client)) == 5
    assert load_cache_config(config_file).token == "test"
    monkeypatch.setenv("TUSHARE_TOKEN", "env-token")
    assert load_cache_config().token == "env-token"
    assert pro_api(token="override", cache_path=cache_path).config.token == "override"


@pytest.mark.parametrize("settings", [
    {"chunk_days": 6000}, {"retries": -1}, {"recent_days": 0}, {"max_symbols": True},
    {"recent_ttl_seconds": float("nan")}, {"request_interval_seconds": -1},
    {"snapshot_ttl_seconds": "300"}, {"lock_timeout_seconds": 0},
])
def test_rejects_invalid_cache_settings(settings):
    with pytest.raises(ValueError):
        CacheConfig(**settings)


def test_upstream_retry_and_rate_limit(cache):
    client, remote, _ = cache
    client.config = replace(client.config, retries=1, request_interval_seconds=0.3)
    sleeps = []
    client._sleep = sleeps.append
    original = remote.__getattr__("daily")

    def flaky(**params):
        if not sleeps:
            raise RuntimeError("temporary")
        return original(**params)

    remote.daily = flaky
    assert len(request(client)) == 5
    assert 1 in sleeps
    assert any(0 < delay < 1 for delay in sleeps)


def test_cache_miss_requires_credentials_but_status_does_not(tmp_path, monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    client = pro_api(token="", cache_path=tmp_path / "cache.duckdb")
    assert client.store.status().empty
    with pytest.raises(ValueError, match="token"):
        request(client)


def test_failed_later_chunk_keeps_successful_chunk_without_covering_failure(cache):
    client, remote, _ = cache
    client.config = replace(client.config, chunk_days=2)
    original = remote.__getattr__("daily")

    def fail_second(**params):
        if params["start_date"] == "20260107":
            raise RuntimeError("second chunk failed")
        return original(**params)

    remote.daily = fail_second
    with pytest.raises(RuntimeError, match="second chunk"):
        request(client)
    remote.daily = original
    request(client)
    assert [(params["start_date"], params["end_date"]) for _, params in remote.calls] == [
        ("20260105", "20260106"), ("20260107", "20260108"), ("20260109", "20260109"),
    ]


@pytest.mark.parametrize("kind", ["none", "duplicate", "date", "null-date", "exchange"])
def test_invalid_remote_response_is_not_cached(cache, kind):
    client, remote, _ = cache
    if kind == "exchange":
        remote.trade_cal = lambda **params: pd.DataFrame(
            [["SZSE", "20260105", "1", "20260102"]], columns=TRADE_CAL_COLS,
        )
        with pytest.raises(ValueError, match="exchange"):
            client.trade_cal(start_date="20260105", end_date="20260105")
    else:
        original = remote.__getattr__("daily")

        def invalid(**params):
            frame = original(**params)
            if kind == "none":
                return None
            if kind == "duplicate":
                return pd.concat([frame, frame])
            frame.loc[0, "trade_date"] = "20260112" if kind == "date" else None
            return frame

        remote.daily = invalid
        with pytest.raises(ValueError):
            request(client)
        remote.daily = original
        assert len(request(client)) == 5


def test_lock_timeout_fails_cleanly_and_recovers(cache):
    client, remote, _ = cache
    other = CachedPro(replace(client.config, lock_timeout_seconds=0.05), upstream=remote, clock=lambda: NOW)
    with client.store.connect():
        with pytest.raises(TimeoutError):
            request(other)
    assert len(request(other)) == 5
    assert len(remote.calls) == 1


def test_rejects_unknown_cache_schema(cache):
    client, _, _ = cache
    with client.store.connect() as connection:
        connection.execute("UPDATE cache_schema SET version = 999")
    with pytest.raises(ValueError, match="schema version"):
        request(client)


def test_future_calendar_has_short_expiry(cache):
    client, remote, clock = cache
    for _ in range(2):
        client.trade_cal(start_date="20270101", end_date="20270102")
    assert len(remote.calls) == 1
    clock[0] += dt.timedelta(seconds=301)
    client.trade_cal(start_date="20270101", end_date="20270102")
    assert len(remote.calls) == 2


def test_null_numeric_fields_and_precision_survive_roundtrip(cache):
    client, remote, _ = cache
    original = remote.__getattr__("daily_basic")

    def nullable(**params):
        frame = original(**params)
        frame["pe"] = None
        frame["close"] = 10.12345678901234
        return frame

    remote.daily_basic = nullable
    first = client.daily_basic(ts_code=SYMBOL, trade_date="20260105")
    second = client.daily_basic(ts_code=SYMBOL, trade_date="20260105", fields="close,pe")
    assert second["pe"].isna().all()
    assert second["close"].iloc[0] == pytest.approx(10.12345678901234, abs=1e-14)
    assert second["close"].iloc[0] == first["close"].iloc[0]
    assert len(remote.calls) == 1
