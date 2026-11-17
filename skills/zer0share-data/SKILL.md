---
name: zer0share-data
description: 使用 zer0share 的请求缓存查询指定 A 股行情、复权因子、每日指标、股票资料和交易日历；缓存缺失时按当前股票与区间访问 Tushare。已有 Parquet 的只读查询需显式选择 LocalPro。
---

# zer0share-data

Use `from zer0share import pro_api` for the current request cache. A cache miss
accesses Tushare and requires a token and the account's API permissions; a full
hit can be read offline. Do not describe the default API as always offline.

Supported cache interfaces: `daily`, `adj_factor`, `daily_basic`, `stock_basic`,
`trade_cal`, `stk_limit`, `stock_st`, `suspend_d`, and `pro_bar`.
Use specific stock codes and explicit date ranges. Dates are
eight-digit year/month/day strings. Do not combine `trade_date` with date ranges.
Do not use a missing-data error as a reason to run the removed sync commands.

```python
from zer0share import pro_api

pro = pro_api()
df = pro.daily(ts_code="600519.SH", start_date="20260101", end_date="20260131")
```

Only request symbols and dates needed for the user's question. No market-wide
or implicit full-history requests are supported. Use `refresh=True` when the
user requests a refresh; it replaces the requested interval, not other data.
Historical empty results are cached, while recent empty results expire.

For Tushare-shaped business code, use `from zer0share import tushare as ts`.
`ts.pro_api(config_path=...)` and `ts.pro_bar(api=client, ...)` use the same cache.
Daily A-share `pro_bar` derives adjustments, MAs, and optional turnover/volume
ratios from cached base APIs. It requires one stock and explicit dates; MAs only
use that interval. There is no monkeypatch of the external Tushare package.
Per-API TTLs live under `[cache.policies.<api>]`. Maintenance commands include
`cache-metrics`, `cache-backup`, `cache-import`, and stock/date-scoped `cache-clear`.

Read `../../docs/guides/query-api.md` for current parameters, expiry, errors, and
unsupported interfaces. Do not infer that every method available in upstream
Tushare has a cache adapter.

The bundled `scripts/query_local.py` now uses this same cache. A missing token,
permission failure, or unsupported interface must be reported without inventing
data or creating a full sync job.

For explicitly offline access to existing Parquet files, use
`LocalPro("/path/to/data")`. Read `references/api.md` only for that legacy mode;
its table identifiers do not imply that the old sync CLI still exists.
