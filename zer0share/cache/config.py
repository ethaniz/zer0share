import math
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class CachePolicy:
    historical_ttl_seconds: float | None = None
    recent_ttl_seconds: float | None = None
    empty_ttl_seconds: float | None = None
    snapshot_ttl_seconds: float | None = None

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0
            ):
                raise ValueError(f"cache policy {name} must be a finite number >= 0")


@dataclass(frozen=True)
class CacheConfig:
    token: str = ""
    path: Path = Path("data/cache.duckdb")
    recent_days: int = 7
    recent_ttl_seconds: float = 300
    snapshot_ttl_seconds: float = 86400
    chunk_days: int = 366
    max_symbols: int = 20
    request_interval_seconds: float = 1.3
    retries: int = 2
    lock_timeout_seconds: float = 120
    policies: dict[str, CachePolicy] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.token, str):
            raise ValueError("tushare.token must be a string")
        for name in ("recent_days", "chunk_days", "max_symbols", "retries"):
            value = getattr(self, name)
            minimum = 0 if name == "retries" else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"cache.{name} must be an integer >= {minimum}")
        if self.chunk_days > 366:
            raise ValueError("cache.chunk_days must be <= 366")
        for name in (
            "recent_ttl_seconds",
            "snapshot_ttl_seconds",
            "request_interval_seconds",
            "lock_timeout_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"cache.{name} must be a finite number")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"cache.{name} must be a finite number >= 0")
        if self.lock_timeout_seconds == 0:
            raise ValueError("cache.lock_timeout_seconds must be > 0")
        supported = {
            "daily", "adj_factor", "daily_basic", "stock_basic", "trade_cal",
            "stk_limit", "suspend_d", "stock_st",
        }
        if not isinstance(self.policies, dict) or set(self.policies) - supported:
            raise ValueError("cache.policies contains unsupported APIs")
        if any(not isinstance(policy, CachePolicy) for policy in self.policies.values()):
            raise ValueError("cache.policies values must be CachePolicy instances")
        for api, policy in self.policies.items():
            if api == "stock_basic":
                if policy.historical_ttl_seconds is not None or policy.recent_ttl_seconds is not None:
                    raise ValueError("stock_basic policy uses snapshot_ttl_seconds")
            elif policy.snapshot_ttl_seconds is not None:
                raise ValueError("snapshot_ttl_seconds only applies to stock_basic")


def load_cache_config(path=None, *, token=None, cache_path=None) -> CacheConfig:
    raw = {}
    if path is not None:
        with Path(path).open("rb") as stream:
            raw = tomllib.load(stream)
    cache = dict(raw.get("cache", {}))
    configured_path = cache.pop("path", "data/cache.duckdb")
    raw_policies = cache.pop("policies", {})
    if not isinstance(raw_policies, dict):
        raise ValueError("cache.policies must be a table")
    policies = {}
    for api, values in raw_policies.items():
        if not isinstance(values, dict) or set(values) - set(CachePolicy.__dataclass_fields__):
            raise ValueError(f"invalid cache policy: {api}")
        policies[api] = CachePolicy(**values)
    known = set(CacheConfig.__dataclass_fields__) - {"token", "path"}
    unknown = set(cache) - known
    if unknown:
        raise ValueError(f"unknown cache settings: {', '.join(sorted(unknown))}")
    configured_token = raw.get("tushare", {}).get("token", "")
    config = CacheConfig(
        token=configured_token or os.getenv("TUSHARE_TOKEN", ""),
        path=Path(configured_path).expanduser(),
        policies=policies,
        **cache,
    )
    if token is not None:
        config = replace(config, token=token)
    if cache_path is not None:
        config = replace(config, path=Path(cache_path).expanduser())
    return config
