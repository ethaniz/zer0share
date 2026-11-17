from dataclasses import replace
import datetime as dt
import json

import duckdb
import pandas as pd
import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from zer0share import CachedPro, CacheConfig, CachePolicy, pro_bar
from zer0share import tushare as cached_tushare
from zer0share.cache import load_cache_config
from zer0share.cache.client import PAGE_LIMITS
from zer0share.schema import STK_LIMIT_COLS, STOCK_ST_COLS, SUSPEND_D_COLS, DAILY_COLS
from tests.test_cache import FakeTushare, NOW, SYMBOL, OTHER_SYMBOL


class StockTushare(FakeTushare):
    def __getattr__(self, api):
        if api not in {"stk_limit", "stock_st", "suspend_d"}:
            original = super().__getattr__(api)

            def fetch_series(**params):
                frame = original(**params)
                if frame.empty or api == "stock_basic" or api == "trade_cal":
                    return frame
                position = pd.to_datetime(frame["trade_date"]).dt.day - 5
                if api == "adj_factor":
                    frame["adj_factor"] = position.map(lambda index: 1.0 if index <= 0 else 2.0)
                elif api == "daily":
                    prices = position.map(lambda index: 100.0 if index <= 0 else 50.0 + (index - 1) * 10)
                    for column in ("open", "high", "low", "close"):
                        frame[column] = prices
                    frame["pre_close"] = position.map(lambda index: 99.0 if index <= 0 else 50.0)
                    frame["vol"] = (position + 1) * 100.0
                return frame
            return fetch_series

        def fetch(**params):
            self.calls.append((api, dict(params)))
            if self.error:
                raise self.error
            columns = {"stk_limit": STK_LIMIT_COLS, "stock_st": STOCK_ST_COLS, "suspend_d": SUSPEND_D_COLS}[api]
            records = []
            if not self.empty:
                for date in pd.date_range(params["start_date"], params["end_date"]):
                    base = {"ts_code": params["ts_code"], "trade_date": date.strftime("%Y%m%d")}
                    if api == "stk_limit":
                        records.append({**base, "pre_close": 10.0, "up_limit": 11.0, "down_limit": 9.0})
                    elif api == "stock_st":
                        for kind in ("ST", "risk"):
                            records.append({**base, "name": "示例", "type": kind, "type_name": kind})
                    else:
                        for kind, timing in (("S", "09:30-10:00"), ("R", None)):
                            records.append({**base, "suspend_type": kind, "suspend_timing": timing})
            frame = pd.DataFrame(records, columns=columns)
            offset = params.get("offset", 0)
            limit = params.get("limit", len(frame))
            return frame.iloc[offset:offset + limit].reset_index(drop=True)
        return fetch


@pytest.fixture
def stock_cache(tmp_path):
    remote = StockTushare()
    config = CacheConfig(path=tmp_path / "cache.duckdb", request_interval_seconds=0, retries=0)
    clock = [NOW]
    return CachedPro(config, upstream=remote, clock=lambda: clock[0]), remote, clock


def bars(client, **params):
    return client.pro_bar(ts_code=SYMBOL, start_date="20260105", end_date="20260107", **params)


@pytest.mark.parametrize("api", ["stk_limit", "stock_st", "suspend_d"])
def test_stock_supplements_fill_only_requested_symbol(stock_cache, api):
    client, remote, _ = stock_cache
    params = dict(ts_code=SYMBOL, start_date="20260105", end_date="20260107")
    first = client.query(api, **params)
    second = client.query(api, **params)
    assert len(first) == (3 if api == "stk_limit" else 6)
    pd.testing.assert_frame_equal(first, second)
    assert len(remote.calls) == 1
    assert remote.calls[0][1]["ts_code"] == SYMBOL
    assert not first.duplicated(list(first.columns)).any()


def test_event_pagination_preserves_same_day_rows(stock_cache, monkeypatch):
    client, remote, _ = stock_cache
    monkeypatch.setitem(PAGE_LIMITS, "suspend_d", 2)
    result = client.suspend_d(ts_code=SYMBOL, start_date="20260105", end_date="20260106")
    assert len(result) == 4
    assert [params["offset"] for _, params in remote.calls] == [0, 2, 4]
    assert len(client.suspend_d(ts_code=SYMBOL, trade_date="20260105", suspend_type="S")) == 1
    assert len(client.suspend_d(ts_code=SYMBOL, trade_date="20260105", suspend_type="R")) == 1
    assert len(remote.calls) == 3
    assert all("suspend_type" not in params for _, params in remote.calls)


def test_server_ignoring_offset_is_not_marked_complete(stock_cache, monkeypatch):
    client, remote, _ = stock_cache
    monkeypatch.setitem(PAGE_LIMITS, "stock_st", 2)
    original = remote.__getattr__("stock_st")

    def repeated(**params):
        params["offset"] = 0
        return original(**params)

    remote.stock_st = repeated
    with pytest.raises(ValueError, match="repeated a page"):
        client.stock_st(ts_code=SYMBOL, trade_date="20260105")
    with client.store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM cache_coverage").fetchone()[0] == 0


@pytest.mark.parametrize("adjustment,expected", [
    ("qfq", [60.0, 50.0, 50.0]),
    ("hfq", [120.0, 100.0, 100.0]),
    (None, [60.0, 50.0, 100.0]),
])
def test_pro_bar_adjusts_prices_not_volumes(stock_cache, adjustment, expected):
    client, remote, _ = stock_cache
    result = bars(client, adj=adjustment)
    assert result["close"].tolist() == expected
    assert result["vol"].tolist() == [300.0, 200.0, 100.0]
    assert {api for api, _ in remote.calls} == ({"daily", "adj_factor"} if adjustment else {"daily"})
    bars(client, adj=adjustment)
    assert len(remote.calls) == (2 if adjustment else 1)


def test_pro_bar_paginates_after_adjustment_and_ma(stock_cache):
    client, remote, _ = stock_cache
    result = bars(client, adj="qfq", ma=[2], factors=["tor", "vr"], adjfactor=True, limit=1, offset=1)
    assert result["close"].tolist() == [50.0]
    assert result["ma2"].tolist() == [50.0]
    assert result["ma_v_2"].tolist() == [150.0]
    assert result["adj_factor"].tolist() == [2.0]
    assert result["turnover_rate"].tolist() == [10.0]
    assert result["volume_ratio"].tolist() == [10.0]
    assert {api for api, _ in remote.calls} == {"daily", "adj_factor", "daily_basic"}
    assert all("limit" not in params for _, params in remote.calls)


def test_qfq_base_uses_end_date_not_local_pagination(stock_cache):
    client, _, _ = stock_cache
    full = bars(client, adj="qfq")
    older = client.pro_bar(ts_code=SYMBOL, start_date="20260105", end_date="20260105", adj="qfq")
    assert older["close"].tolist() == [100.0]
    assert full.iloc[-1]["close"] == 50.0


def test_pro_bar_validates_before_any_remote_call(stock_cache):
    client, remote, _ = stock_cache
    for kwargs in (
        {"asset": "I"}, {"freq": "W"}, {"adj": "invalid"}, {"ma": [0]},
        {"ma": [True]}, {"factors": ["unknown"]}, {"fields": "unknown"}, {"adjfactor": True},
    ):
        with pytest.raises((ValueError, NotImplementedError)):
            bars(client, **kwargs)
    assert remote.calls == []


def test_pro_bar_missing_factors_fails_without_fabrication(stock_cache):
    client, remote, _ = stock_cache
    remote.adj_factor = lambda **params: pd.DataFrame()
    with pytest.raises(ValueError, match="missing or invalid"):
        bars(client, adj="qfq")


def test_pro_bar_empty_returns_requested_columns(stock_cache):
    client, remote, _ = stock_cache
    remote.empty = True
    result = bars(client, adj="qfq", ma=[2], fields="trade_date,close,ma2")
    assert result.empty
    assert list(result.columns) == ["trade_date", "close", "ma2"]
    assert len(remote.calls) == 1


def test_top_level_and_sdk_shaped_adapter_use_cache(stock_cache, monkeypatch):
    client, remote, _ = stock_cache
    result = pro_bar(ts_code=SYMBOL, api=client, start_date="20260105", end_date="20260107", adj="qfq")
    pd.testing.assert_frame_equal(result, bars(client, adj="qfq"))
    assert len(remote.calls) == 2
    captured = {}

    def factory(config_path, **kwargs):
        captured.update(config_path=config_path, **kwargs)
        return client

    monkeypatch.setattr(cached_tushare, "_pro_api", factory)
    assert cached_tushare.pro_api("test-token", config_path="settings.toml") is client
    assert captured["token"] == "test-token"
    assert captured["config_path"] == "settings.toml"
    with pytest.raises(TypeError, match="CachedPro"):
        cached_tushare.pro_bar(ts_code=SYMBOL, api=object())


@pytest.mark.parametrize("adjustment", [None, "qfq", "hfq"])
def test_adjustment_matches_installed_tushare_sdk(stock_cache, adjustment):
    import tushare

    client, remote, _ = stock_cache
    ours = bars(client, adj=adjustment, ma=[2], factors=["tor", "vr"])
    before = len(remote.calls)
    theirs = tushare.pro_bar(
        ts_code=SYMBOL, api=client, start_date="20260105", end_date="20260107",
        adj=adjustment, ma=[2], factors=["tor", "vr"], retry_count=1,
    )
    pd.testing.assert_frame_equal(
        ours, theirs.loc[:, ours.columns], check_dtype=False, rtol=1e-10, atol=1e-10,
    )
    assert len(remote.calls) == before


def test_old_cache_schema_upgrade_preserves_rows_and_coverage(tmp_path):
    path = tmp_path / "old.duckdb"
    payload = dict.fromkeys(DAILY_COLS, 10.0)
    payload.update(ts_code=SYMBOL, trade_date="20260105")
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE cache_schema (version INTEGER)")
        connection.execute("INSERT INTO cache_schema VALUES (1)")
        connection.execute("""
            CREATE TABLE cache_rows (
                api VARCHAR, scope VARCHAR, record_date VARCHAR, payload VARCHAR,
                PRIMARY KEY (api, scope, record_date)
            )
        """)
        connection.execute("INSERT INTO cache_rows VALUES ('daily', ?, '20260105', ?)", [SYMBOL, json.dumps(payload)])
        connection.execute("""
            CREATE TABLE cache_coverage (
                api VARCHAR, scope VARCHAR, start_date VARCHAR, end_date VARCHAR, expires_at DOUBLE,
                PRIMARY KEY (api, scope, start_date, end_date)
            )
        """)
        connection.execute("INSERT INTO cache_coverage VALUES ('daily', ?, '20260105', '20260105', NULL)", [SYMBOL])
    remote = StockTushare()
    client = CachedPro(CacheConfig(path=path), upstream=remote, clock=lambda: NOW)
    assert client.daily(ts_code=SYMBOL, trade_date="20260105")["close"].tolist() == [10.0]
    assert remote.calls == []
    with client.store.connect() as connection:
        assert connection.execute("SELECT version FROM cache_schema").fetchall() == [(2,)]


def test_clear_middle_interval_preserves_edges_and_other_symbols(stock_cache):
    client, remote, _ = stock_cache
    for symbol in (SYMBOL, OTHER_SYMBOL):
        client.daily(ts_code=symbol, start_date="20260105", end_date="20260109")
    client.clear_cache("daily", ts_code=SYMBOL, start_date="20260107", end_date="20260107")
    client.daily(ts_code=OTHER_SYMBOL, start_date="20260105", end_date="20260109")
    result = client.daily(ts_code=SYMBOL, start_date="20260105", end_date="20260109")
    assert len(result) == 5
    assert len(remote.calls) == 3
    assert remote.calls[-1][1]["start_date"] == remote.calls[-1][1]["end_date"] == "20260107"


def test_clear_snapshot_is_scoped(stock_cache):
    client, remote, _ = stock_cache
    client.stock_basic(ts_code=[SYMBOL, OTHER_SYMBOL])
    client.clear_cache("stock_basic", ts_code=SYMBOL)
    client.stock_basic(ts_code=OTHER_SYMBOL)
    client.stock_basic(ts_code=SYMBOL)
    assert len(remote.calls) == 3
    with pytest.raises(ValueError, match="snapshot"):
        client.clear_cache("stock_basic", start_date="20260101")


def test_metrics_count_miss_hit_partial_and_remote_errors(stock_cache):
    client, remote, _ = stock_cache
    client.daily(ts_code=SYMBOL, trade_date="20260105")
    client.daily(ts_code=SYMBOL, trade_date="20260105")
    client.daily(ts_code=SYMBOL, start_date="20260105", end_date="20260106")
    remote.error = RuntimeError("unavailable")
    with pytest.raises(RuntimeError):
        client.daily(ts_code=SYMBOL, trade_date="20260107")
    stats = client.store.metrics().set_index("api").loc["daily"]
    assert stats["lookups"] == 4
    assert stats["hits"] == stats["partial_hits"] == stats["remote_errors"] == 1
    assert stats["misses"] == 2
    assert stats["remote_calls"] == 3
    assert stats["hit_rate"] == 0.25


def test_per_api_policy_expires_history_and_negative_results(stock_cache):
    client, remote, clock = stock_cache
    client.config = replace(client.config, policies={"daily": CachePolicy(
        historical_ttl_seconds=60, empty_ttl_seconds=10,
    )})
    remote.empty = True
    assert client.daily(ts_code=SYMBOL, trade_date="20260105").empty
    clock[0] += dt.timedelta(seconds=11)
    remote.empty = False
    assert len(client.daily(ts_code=SYMBOL, trade_date="20260105")) == 1
    clock[0] += dt.timedelta(seconds=61)
    client.daily(ts_code=SYMBOL, trade_date="20260105")
    assert len(remote.calls) == 3


def test_stock_basic_policy_and_toml_loading(stock_cache, tmp_path):
    client, remote, clock = stock_cache
    client.config = replace(client.config, policies={
        "stock_basic": CachePolicy(snapshot_ttl_seconds=30, empty_ttl_seconds=10),
    })
    client.stock_basic(ts_code=SYMBOL)
    clock[0] += dt.timedelta(seconds=31)
    client.stock_basic(ts_code=SYMBOL)
    assert len(remote.calls) == 2
    config = tmp_path / "settings.toml"
    config.write_text("[cache.policies.daily]\nempty_ttl_seconds=60\n")
    assert load_cache_config(config).policies["daily"].empty_ttl_seconds == 60


def test_partial_refresh_ttl_is_not_hidden_by_old_coverage(stock_cache):
    client, remote, clock = stock_cache
    client.daily(ts_code=SYMBOL, start_date="20260105", end_date="20260109")
    client.config = replace(client.config, policies={
        "daily": CachePolicy(historical_ttl_seconds=10),
    })
    client.daily(ts_code=SYMBOL, trade_date="20260107", refresh=True)
    clock[0] += dt.timedelta(seconds=11)
    client.daily(ts_code=SYMBOL, start_date="20260105", end_date="20260109")
    assert len(remote.calls) == 3
    assert remote.calls[-1][1]["start_date"] == remote.calls[-1][1]["end_date"] == "20260107"


@pytest.mark.parametrize("policy", [
    {"daily": CachePolicy(snapshot_ttl_seconds=1)},
    {"stock_basic": CachePolicy(historical_ttl_seconds=1)},
    {"unknown": CachePolicy()},
])
def test_rejects_inapplicable_policies(policy):
    with pytest.raises(ValueError):
        CacheConfig(policies=policy)


def test_backup_is_consistent_and_never_overwrites(stock_cache, tmp_path):
    client, remote, _ = stock_cache
    expected = client.daily(ts_code=SYMBOL, trade_date="20260105")
    backup = client.store.backup(tmp_path / "backup.duckdb")
    restored = CachedPro(replace(client.config, path=backup), clock=lambda: NOW)
    pd.testing.assert_frame_equal(restored.daily(ts_code=SYMBOL, trade_date="20260105"), expected)
    with pytest.raises(FileExistsError):
        client.store.backup(backup)
    with pytest.raises(ValueError):
        client.store.backup(client.store.path)
    assert len(remote.calls) == 1


def write_partition(root, api_directory, date, frame):
    path = root / "stock" / api_directory / f"date={date}" / "data.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), path)


def test_parquet_import_does_not_cover_holes_or_other_stocks(stock_cache, tmp_path):
    client, remote, _ = stock_cache
    source = tmp_path / "old"
    frame = remote.daily(ts_code=SYMBOL, start_date="20260105", end_date="20260105")
    write_partition(source, "daily_kline", "20260105", frame)
    remote.calls.clear()
    report = client.import_parquet(source, "daily", SYMBOL, "20260105", "20260107")
    assert report["imported_dates"] == report["imported_rows"] == 1
    assert report["missing_partitions"] == 2
    assert len(client.daily(ts_code=SYMBOL, trade_date="20260105")) == 1
    assert remote.calls == []
    client.daily(ts_code=SYMBOL, start_date="20260105", end_date="20260107")
    assert remote.calls[-1][1]["start_date"] == "20260106"
    client.daily(ts_code=OTHER_SYMBOL, trade_date="20260105")
    assert len(remote.calls) == 2


def test_import_preserves_existing_cache_and_rejects_bad_source(stock_cache, tmp_path):
    client, remote, _ = stock_cache
    client.daily(ts_code=SYMBOL, trade_date="20260105")
    frame = remote.daily(ts_code=SYMBOL, start_date="20260105", end_date="20260105")
    frame["close"] = 999
    write_partition(tmp_path / "old", "daily_kline", "20260105", frame)
    report = client.import_parquet(tmp_path / "old", "daily", SYMBOL, "20260105", "20260105")
    assert report["existing_dates"] == 1
    assert client.daily(ts_code=SYMBOL, trade_date="20260105")["close"].tolist() == [100.0]
    frame = frame.drop(columns="close")
    write_partition(tmp_path / "bad", "daily_kline", "20260105", frame)
    with pytest.raises(ValueError, match="incomplete"):
        client.import_parquet(tmp_path / "bad", "daily", SYMBOL, "20260105", "20260105")


def test_empty_partition_does_not_certify_negative_coverage(stock_cache, tmp_path):
    client, remote, _ = stock_cache
    write_partition(tmp_path, "daily_kline", "20260105", pd.DataFrame(columns=DAILY_COLS))
    report = client.import_parquet(tmp_path, "daily", SYMBOL, "20260105", "20260105")
    assert report["empty_dates"] == 1
    client.daily(ts_code=SYMBOL, trade_date="20260105")
    assert len(remote.calls) == 1
