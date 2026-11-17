import argparse

from zer0share import tushare as ts


def main():
    parser = argparse.ArgumentParser(description="以 Tushare 风格调用请求缓存和复权行情。")
    parser.add_argument("--ts-code", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--adj", choices=["qfq", "hfq"], default="qfq")
    args = parser.parse_args()
    client = ts.pro_api(config_path=args.config)
    result = ts.pro_bar(
        ts_code=args.ts_code, api=client, start_date=args.start_date,
        end_date=args.end_date, adj=args.adj,
    )
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
