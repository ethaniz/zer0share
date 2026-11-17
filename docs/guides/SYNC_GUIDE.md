# 旧同步功能已退出主流程

当前版本按查询请求更新缓存，不再提供 `sync --all`、`sync --table`、市场分组同步和定时调度 CLI。

使用方法见 [项目 README](../../README.md) 和 [请求缓存 API](query-api.md)：

```python
from zer0share import pro_api

pro = pro_api()
df = pro.daily(ts_code="600519.SH", start_date="20260101", end_date="20260131")
```

缓存未覆盖时只拉当前股票缺失的日期范围，完整命中时不访问 Tushare。

旧同步实现保留为内部历史兼容代码，不作为新的公共更新入口。旧服务需要停止后再使用新版本；不需要部署 launchd、cron、systemd 或常驻容器。
