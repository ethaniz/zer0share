import json

from click.testing import CliRunner

from zer0share.cli import cli
from zer0share import pro_api
from tests.test_cache import FakeTushare


def test_query_cli_uses_persistent_cache(tmp_path, monkeypatch):
    client = pro_api(cache_path=tmp_path / "cache.duckdb", upstream=FakeTushare())
    monkeypatch.setattr("zer0share.cli.pro_api", lambda config: client)
    runner = CliRunner()
    arguments = ["query", "daily", "--params", json.dumps({
        "ts_code": "600519.SH", "trade_date": "20260105", "fields": "trade_date,close",
    })]
    first = runner.invoke(cli, arguments)
    second = runner.invoke(cli, arguments)
    assert first.exit_code == second.exit_code == 0
    assert json.loads(first.output) == [{"trade_date": "20260105", "close": 10.0}]
    assert len(client._upstream.calls) == 1
    status = runner.invoke(cli, ["cache-status"])
    assert status.exit_code == 0
    assert json.loads(status.output)[0]["api"] == "daily"
    assert runner.invoke(cli, ["cache-clear", "--api", "daily", "--yes"]).exit_code == 0
    runner.invoke(cli, arguments)
    assert len(client._upstream.calls) == 2


def test_cli_rejects_bulk_sync_and_scheduler():
    runner = CliRunner()
    for arguments in (["sync", "--all"], ["scheduler", "start"]):
        result = runner.invoke(cli, arguments)
        assert result.exit_code != 0
        assert "No such command" in result.output


def test_cli_errors_are_actionable():
    runner = CliRunner()
    for params in ("[]", "{", '{"ts_code": "600519.SH"}'):
        result = runner.invoke(cli, ["query", "daily", "--params", params])
        assert result.exit_code != 0
        assert "Error:" in result.output


def test_cli_cache_maintenance_is_local(tmp_path, monkeypatch):
    client = pro_api(cache_path=tmp_path / "cache.duckdb", upstream=FakeTushare())
    monkeypatch.setattr("zer0share.cli.pro_api", lambda config: client)
    client.daily(ts_code="000001.SZ", start_date="20260105", end_date="20260109")
    runner = CliRunner()
    metrics = runner.invoke(cli, ["cache-metrics"])
    assert metrics.exit_code == 0
    assert json.loads(metrics.output)["metrics"][0]["remote_calls"] == 1
    backup = tmp_path / "backup.duckdb"
    assert runner.invoke(cli, ["cache-backup", str(backup)]).exit_code == 0
    assert backup.is_file()
    assert runner.invoke(cli, ["cache-backup", str(backup)]).exit_code != 0
    cleared = runner.invoke(cli, [
        "cache-clear", "--api", "daily", "--ts-code", "000001.SZ",
        "--start-date", "20260107", "--end-date", "20260107", "--yes",
    ])
    assert cleared.exit_code == 0
    client.daily(ts_code="000001.SZ", start_date="20260105", end_date="20260109")
    assert len(client._upstream.calls) == 2
    assert client._upstream.calls[-1][1]["start_date"] == "20260107"


def test_cli_import_does_not_fetch_remote(tmp_path, monkeypatch):
    import pandas as pd

    from tests.test_cache_phase2 import write_partition
    from zer0share.schema import DAILY_COLS

    source = tmp_path / "old"
    write_partition(source, "daily_kline", "20260105", pd.DataFrame(columns=DAILY_COLS))
    client = pro_api(token="", cache_path=tmp_path / "cache.duckdb")
    monkeypatch.setattr("zer0share.cli.pro_api", lambda config: client)
    result = CliRunner().invoke(cli, [
        "cache-import", "--source", str(source), "--api", "daily",
        "--ts-code", "000001.SZ", "--start-date", "20260105", "--end-date", "20260106",
    ])
    assert result.exit_code == 0
    assert json.loads(result.output)["missing_partitions"] == 1
    assert client.store.status().empty
