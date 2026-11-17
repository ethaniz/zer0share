#!/usr/bin/env python3
"""Run a request-driven zer0share cached query from the command line."""

from __future__ import annotations

import argparse
import json

from zer0share import pro_api


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query zer0share; fetch only missing data from Tushare.")
    parser.add_argument("api_name", help="Cached API: daily, adj_factor, daily_basic, stock_basic, trade_cal.")
    parser.add_argument(
        "--params",
        default="{}",
        help="JSON object passed as keyword arguments to pro.query.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional path to zer0share settings.toml.",
    )
    parser.add_argument(
        "--head",
        type=int,
        default=20,
        help="Print the first N rows. Use 0 to print all rows.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    params = json.loads(args.params)
    if not isinstance(params, dict):
        raise TypeError("--params must be a JSON object")

    pro = pro_api(args.config)
    df = pro.query(args.api_name, **params)

    print(f"api={args.api_name} rows={len(df)} columns={list(df.columns)}")
    preview = df if args.head == 0 else df.head(args.head)
    print(preview.to_string(index=False))


if __name__ == "__main__":
    main()
