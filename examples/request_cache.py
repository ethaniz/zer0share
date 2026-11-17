import argparse

from zer0share import pro_api


def main():
    parser = argparse.ArgumentParser(description="按股票与日期请求行情，缺失部分自动写入缓存。")
    parser.add_argument("--ts-code", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    client = pro_api(args.config)
    result = client.daily(
        ts_code=args.ts_code,
        start_date=args.start_date,
        end_date=args.end_date,
        refresh=args.refresh,
    )
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
