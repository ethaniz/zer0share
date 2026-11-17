# zer0share

按请求更新的 Tushare 本地缓存。请求先查本地，只向 Tushare 获取当前股票、当前日期范围中缺失的数据，成功后写入 DuckDB，供下次复用。

无需全量同步，也没有后台调度器。默认查询入口是 `pro_api()`。

## 快速开始

```bash
uv sync --frozen --python 3.12
cp -n config/settings.example.toml config/settings.toml
chmod 600 config/settings.toml
```

在配置的 `[tushare]` 中填写自己的 Token，或设置 `TUSHARE_TOKEN` 环境变量。没有配置文件时也可以直接使用环境变量；缓存默认保存到当前目录下的 `data/cache.duckdb`。

```python
from zer0share import pro_api

pro = pro_api()
prices = pro.daily(
    ts_code="600519.SH",
    start_date="20260101",
    end_date="20260131",
    fields="ts_code,trade_date,close,vol",
)

# 完整命中时不会请求 Tushare。
same_prices = pro.daily(
    ts_code="600519.SH",
    start_date="20260101",
    end_date="20260131",
    fields="ts_code,trade_date,close,vol",
)

# 仅补齐 2 月缺失部分。
more_prices = pro.daily(
    ts_code="600519.SH",
    start_date="20260101",
    end_date="20260228",
)
```

也支持 `pro_api(token=..., cache_path=...)` 和指定配置文件 `pro_api("config/settings.toml")`。Token 仅在缓存缺失时需要，完整缓存可离线读取。

## 支持接口

| 接口 | 查询范围 |
|---|---|
| `daily` | 单只或少量 A 股的未复权日线 |
| `adj_factor` | 复权因子 |
| `daily_basic` | 每日指标 |
| `stock_basic` | 指定股票的基础信息快照 |
| `trade_cal` | 指定交易所及日期区间的日历 |
| `stk_limit` | 指定股票的每日涨跌停价格 |
| `stock_st` | 指定股票的特别处理记录 |
| `suspend_d` | 指定股票的停复牌记录 |
| `pro_bar` | 从缓存日线计算不复权、前复权或后复权行情、均线和指标 |

行情和股票基础信息必须提供具体 `ts_code`，支持逗号分隔字符串或列表，每次默认最多 20 只。行情必须提供 `trade_date` 或 `start_date`；省略 `end_date` 时使用上海时区的今天。禁止隐式全市场请求、隐式全历史请求和未来行情日期。

`fields` 只裁剪返回列；内部缓存每个适配接口的固定字段集合。行情结果按日期倒序、代码升序返回，`limit` 和 `offset` 在本地完整缓存结果上分页。尚未适配的接口明确抛出 `NotImplementedError`，不会偷偷触发全量下载或回退到旧数据。

完整参数见 [查询 API](docs/guides/query-api.md)。

业务代码可使用项目内的 Tushare 风格入口：

```python
from zer0share import tushare as ts

pro = ts.pro_api(config_path="config/settings.toml")
bars = ts.pro_bar(
    ts_code="000001.SZ", api=pro, start_date="20260101", end_date="20260131",
    adj="qfq", ma=[5, 10], factors=["tor", "vr"],
)
```

这需要显式修改业务代码的 import，并不替换系统安装的 `tushare` 包。`pro_bar` 当前限单只 A 股、日频，复用 `daily`、`adj_factor` 和按需获取的 `daily_basic`；前复权基准取请求区间内最新因子，均线仅使用请求区间内数据。示例见 `examples/tushare_cached.py`。

## 缓存与刷新

- 较早历史数据和历史空结果长期保留；最近 7 个自然日的数据和空结果默认 5 分钟后过期。
- 股票基础信息默认缓存 24 小时。
- `refresh=True` 重新获取本次请求区间并替换对应记录，可用于历史数据修订。
- 仅成功且字段完整的响应会写入覆盖记录；异常、权限错误和错误响应不会被记成缓存命中。
- 单股票区间拆分为最多 366 个自然日的请求，覆盖元数据按接口、股票、区间独立记录。
- 同一缓存库的远程请求默认至少间隔 1.3 秒，可根据账户权限调整；其他程序或其他缓存库的请求不计入该限流。
- 数据和覆盖记录在同一事务内更新。Mac/Linux 本地文件锁防止多个进程重复回填同一缓存；同一数据库内请求串行执行，等待超时会明确报错。

近期过期数据只会在下次请求时刷新，没有定时任务。历史修订也不会自动扫描，需要显式刷新。成功的历史空结果表示接口当时未提供记录，并不能证明该日期必然无交易。

参数可在 `[cache]` 中调整，见 [配置示例](config/settings.example.toml)。

也可在 `[cache.policies.daily]` 等接口表中分别设置 `historical_ttl_seconds`、`recent_ttl_seconds` 和 `empty_ttl_seconds`；`stock_basic` 使用 `snapshot_ttl_seconds`。历史 TTL 未配置时长期保留；空结果 TTL 如配置则取更短有效期。`pro_bar` 使用各基础接口的策略。

## 命令行

```bash
uv run python main.py query daily \
  --params '{"ts_code":"600519.SH","start_date":"20260101","end_date":"20260131"}'

uv run python main.py query daily \
  --params '{"ts_code":"600519.SH","trade_date":"20260105"}' --refresh

uv run python main.py cache-status
uv run python main.py cache-clear --api daily
uv run python main.py cache-metrics
uv run python main.py cache-clear --api daily --ts-code 000001.SZ \
  --start-date 20260105 --end-date 20260109
uv run python main.py cache-backup backups/cache.duckdb
uv run python main.py cache-import --source /path/to/old/data \
  --api daily --ts-code 000001.SZ --start-date 20260101 --end-date 20260131
```

`cache-clear` 默认需要确认，脚本调用可加 `--yes`；不传任何范围参数时清除全部缓存。可按接口、单只股票和日期区间清理；快照不适用日期区间。清理保留累计指标。`cache-metrics` 返回各接口命中率、远程调用/错误次数及数据库大小；完整命中率不包含部分命中。备份在缓存锁内执行检查点后复制，拒绝覆盖已有文件。维护命令不访问 Tushare。指定配置文件使用 `python main.py --config PATH query ...`。

## 旧版迁移

这是从全量采集管道到请求缓存的接口变更：

- 已移除 `scheduler start`、`sync --all` 等旧 CLI、APScheduler 依赖和常驻容器/systemd 部署。
- 旧 Parquet 文件没有覆盖范围证据，不会自动导入缓存或冒充完整命中。
- `cache-import` 可显式导入指定股票、接口和日期范围的现有分区；只认证实际存在且通过校验的非空股票日期，缺失分区和空分区下次仍请求 Tushare。已有缓存覆盖优先保留。
- 需要读已有 Parquet 时，显式使用 `LocalPro("/path/to/old/data")`，或使用旧配置调用 `local_api(...)`。旧的查询、质检、新闻和 RiceQuant 库代码保留用于现有数据；它们尚未接入请求缓存。
- 旧离线示例保留在 `examples/`，已显式切换为 `local_api()`；新示例是 `examples/request_cache.py`。
- `docs/dev/` 中旧设计和 `docs/reports/` 中旧报告为历史资料，不是当前操作说明。

安装了旧后台服务的环境需要先自行停止并移除旧服务；新版本不会自动修改系统服务。本次本机此前没有启用该服务。

## 验证

```bash
uv run python -m pytest -q
```

新缓存测试使用可控的数据源替身，覆盖缓存命中、缺口补齐、过期、空结果、刷新、事务回滚、线程和进程并发。真实接口仍需要自己的 Token 和对应权限；缓存缺失的请求会使用账户接口频次。

存储与备份细节见 [存储说明](docs/guides/storage.md)。
