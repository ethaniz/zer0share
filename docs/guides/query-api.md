# 请求缓存 API

## 创建客户端

```python
from zer0share import pro_api

pro = pro_api()
pro = pro_api("config/settings.toml")
pro = pro_api(token="自己的 Token", cache_path="/path/to/cache.duckdb")
```

优先级为显式 `token` 参数、配置文件中非空 Token、`TUSHARE_TOKEN` 环境变量。默认尝试当前工作目录的 `config/settings.toml`；不存在时使用环境变量和默认缓存配置。显式指定的配置文件不存在时直接报错。

缓存文件、覆盖信息和限流时间均持久化。客户端不持有长期数据库连接，无需手动关闭。库不启动任何后台线程或调度器。

## 行情接口

`daily`、`adj_factor`、`daily_basic`、`stk_limit`、`stock_st` 参数一致：

```python
df = pro.daily(
    ts_code="600519.SH,601318.SH",
    start_date="20260101",
    end_date="20260131",
    fields="ts_code,trade_date,close",
    limit=10,
    offset=0,
    refresh=False,
)
```

- `ts_code` 必填，可以是逗号分隔字符串或列表，支持 SH/SZ/BJ 后缀；空格、大小写和重复代码会标准化。默认最多 20 只。
- 日期必须是有效的八位年月日字符串。必须提供 `start_date` 或 `trade_date`，两者不能混用。
- 提供 `start_date` 时，省略 `end_date` 使用上海时区今天。不能查询未来行情。
- `trade_date` 只查询一天。
- `fields` 为逗号分隔字符串或字段列表，只能选择当前适配器固定字段集合内的字段。未知字段提前报错。并非支持上游今后新增的所有字段。
- `limit` 和 `offset` 为非负整数，仅在本地分页，不会把截断的远程响应当成完整缓存。
- `refresh=True` 无视当前覆盖记录，重新获取本次股票和日期范围；其他股票及区间不变。
- 默认返回完整固定字段集合，按交易日期倒序、同日股票代码升序排列。

通过 `pro.query("daily", **params)` 可使用同样的逻辑。

缓存只拉请求中的股票，不会顺带拉取交易日历、基础信息、指数或其他股票。日期拆分后的单次请求最多覆盖 366 个自然日，每次仅一只股票；无需通过全市场扫描获得少量股票。

`suspend_d` 具有同样的股票和日期参数，并支持 `suspend_type="S"`（停牌）或 `"R"`（复牌）；过滤在完整缓存上执行。`stock_st` 和 `suspend_d` 支持远程分页与同日多条事件，重复页会报错而不会记为完整覆盖。

## 复权行情与业务接入

```python
from zer0share import tushare as ts

pro = ts.pro_api(config_path="config/settings.toml")
df = ts.pro_bar(
    ts_code="000001.SZ", api=pro, start_date="20260101", end_date="20260131",
    adj="qfq", ma=[5, 10], factors=["tor", "vr"], adjfactor=True,
)
```

也可调用 `pro.pro_bar(...)` 或 `pro.query("pro_bar", ...)`。限单只 A 股、`asset="E"`、`freq="D"`，`adj` 支持 `None`、`"qfq"` 和 `"hfq"`；同样要求显式日期范围，支持 `trade_date`、`fields`、`limit`、`offset` 和 `refresh`。

先缓存日线，复权时缓存同区间复权因子，`tor`/`vr` 从每日指标取得 `turnover_rate`/`volume_ratio`。派生结果不另行存储；缺少复权因子时报错。前复权以请求区间内最新因子为基准；均线按区间内数据计算，不额外向前拉取，前几行可能为空。分页在复权和均线计算完成后执行。`adjfactor=True` 需要同时指定复权方式。

项目内 `zer0share.tushare.pro_api()` 的首个参数是 Token，配置路径用 `config_path=`；顶层 `zer0share.pro_api()` 首个参数仍为配置路径。两者返回同一种缓存客户端，不修改外部 Tushare 包。

## 接口有效期

在 `[cache.policies.<接口名>]` 配置 `recent_ttl_seconds`、`historical_ttl_seconds`、`empty_ttl_seconds`。未设置的近期策略继承全局配置，未设置的历史 TTL 表示长期保留。空结果 TTL 若设置则取其与正常 TTL 的较小值。快照接口 `stock_basic` 使用 `snapshot_ttl_seconds` 和可选空结果 TTL。派生 `pro_bar` 遵循基础接口策略。有效期为非负秒数，0 表示下次请求即过期。

## 股票基础信息

```python
df = pro.stock_basic(ts_code="600519.SH", fields="ts_code,name,list_date")
```

支持 `list_status`（默认 `"L"`）、`exchange`、`market`、`is_hs`、`name` 过滤，以及 `fields`、`limit`、`offset`、`refresh`。`ts_code` 同样必须指定。不同过滤条件的结果分别缓存，默认 24 小时有效。

## 交易日历

```python
df = pro.trade_cal(
    exchange="SSE",
    start_date="20260101",
    end_date="20260131",
    is_open=1,
)
```

必须明确起始日期，默认交易所为 SSE。只请求该交易所的指定日期范围，不遍历所有交易所。`is_open` 在完整区间缓存上过滤，支持 `0`、`1` 及对应字符串；也支持 `fields`、`limit`、`offset`、`refresh`。

日历允许未来日期；近期和未来日历都采用短有效期，后续请求时可更新。

## 缓存维护

```python
print(pro.store.status())
print(pro.store.metrics())
pro.clear_cache("daily", ts_code="000001.SZ", start_date="20260105", end_date="20260109")
pro.store.backup("backups/cache.duckdb")
pro.import_parquet("/path/to/old/data", "daily", "000001.SZ", "20260101", "20260131")
```

`status()` 返回接口、数据行数、查询范围数量和覆盖区间数；有效期已经过期的条目仍计入已存储统计，查询时会重新检查有效性。空结果也有覆盖记录，因此可能存在零行的接口。

清理会同时移除数据和覆盖记录，并与其他缓存操作互斥；DuckDB 的逻辑删除不保证立即缩小数据库文件。

`clear_cache()` 校验股票及日期参数；清理中间区间会保留两侧覆盖，不触及其他股票。`stock_basic` 不接受日期清理。统计为持久化累计值，清理数据不会重置；`hit_rate` 是完整命中数除以查询数，部分命中单独计数。`pro_bar` 计入其基础接口的查询统计。

## 边界和错误

不支持的接口和动态名称会抛出 `NotImplementedError`。行情请求未指定股票/起始日期、未知字段和错误日期会在访问远程接口前报错。

网络或权限错误向调用者传递，不返回“成功的空缓存”。过期数据更新失败时也报错，不静默返回旧数据。一次大请求的早先成功分块可保留，失败分块没有覆盖记录，下次可接着补齐。

每个响应必须包含固定字段集合，股票和日期必须符合请求；日行情同一股票同日不能重复，事件接口按事件键区分同日记录。`None` 等非 DataFrame 响应不缓存。成功空结果按配置策略缓存；如怀疑上游暂时返回空结果，可使用 `refresh=True`。

默认历史缓存不自动跟踪上游历史修订。`daily` 价格未复权，复权使用 `pro_bar`；当前不支持 ETF、指数、财务、新闻或 RiceQuant 缓存。旧数据只读入口是 `LocalPro(data_dir)`，两种模式的完整性保证不同。
