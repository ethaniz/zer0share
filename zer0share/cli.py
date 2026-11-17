import json

import click

from zer0share import pro_api
from zer0share.cache.client import DAILY_APIS, STORED_APIS, SUPPORTED_APIS


@click.group()
@click.option("--config", type=click.Path(exists=True, dir_okay=False), default=None)
@click.pass_context
def cli(ctx, config):
    """按请求查询 Tushare，自动缓存命中结果并补齐缺失区间。"""
    ctx.ensure_object(dict)
    ctx.obj["config"] = config


@cli.command("query")
@click.argument("api", type=click.Choice(SUPPORTED_APIS))
@click.option("--params", default="{}", help="Tushare 查询参数，JSON 对象。")
@click.option("--refresh", is_flag=True, help="重新拉取本次请求范围。")
@click.pass_context
def query(ctx, api, params, refresh):
    """发起一次查询，将结果输出为 JSON。"""
    try:
        parameters = json.loads(params)
        if not isinstance(parameters, dict):
            raise ValueError("--params must be a JSON object")
        if refresh:
            parameters["refresh"] = True
        result = pro_api(ctx.obj["config"]).query(api, **parameters)
        click.echo(result.to_json(orient="records", force_ascii=False))
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("cache-status")
@click.pass_context
def cache_status(ctx):
    """查看本地缓存统计，不请求 Tushare。"""
    try:
        result = pro_api(ctx.obj["config"]).store.status()
        click.echo(result.to_json(orient="records", force_ascii=False))
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("cache-clear")
@click.option("--api", type=click.Choice(STORED_APIS), default=None)
@click.option("--ts-code", default=None)
@click.option("--start-date", default=None)
@click.option("--end-date", default=None)
@click.confirmation_option(prompt="确认清除指定缓存？")
@click.pass_context
def cache_clear(ctx, api, ts_code, start_date, end_date):
    """清理一个接口或全部缓存。下次查询将重新获取。"""
    try:
        pro_api(ctx.obj["config"]).clear_cache(
            api, ts_code=ts_code, start_date=start_date, end_date=end_date,
        )
        click.echo("缓存已清除")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("cache-metrics")
@click.pass_context
def cache_metrics(ctx):
    """查看持久化命中率和远程调用次数。"""
    try:
        client = pro_api(ctx.obj["config"])
        metrics = client.store.metrics().to_dict(orient="records")
        click.echo(json.dumps({
            "path": str(client.store.path),
            "size_bytes": client.store.path.stat().st_size,
            "metrics": metrics,
        }, ensure_ascii=False))
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("cache-backup")
@click.argument("destination", type=click.Path(dir_okay=False))
@click.pass_context
def cache_backup(ctx, destination):
    """检查点落盘后复制数据库；拒绝覆盖已有备份。"""
    try:
        path = pro_api(ctx.obj["config"]).store.backup(destination)
        click.echo(str(path))
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("cache-import")
@click.option("--source", type=click.Path(exists=True, file_okay=False), required=True)
@click.option("--api", type=click.Choice(tuple(DAILY_APIS)), required=True)
@click.option("--ts-code", required=True)
@click.option("--start-date", required=True)
@click.option("--end-date", required=True)
@click.pass_context
def cache_import(ctx, source, api, ts_code, start_date, end_date):
    """显式导入已存在的股票日期分区，不推断缺失日期已完整。"""
    try:
        report = pro_api(ctx.obj["config"]).import_parquet(source, api, ts_code, start_date, end_date)
        click.echo(json.dumps(report, ensure_ascii=False))
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
