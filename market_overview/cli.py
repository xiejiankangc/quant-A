"""命令行入口：抓取、报告、仓库初始化与状态。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .fetch import fetch_all
from .portfolio import Portfolio
from .report import render
from .store import Store


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market_overview")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化本地 DuckDB 规范仓库")
    init.add_argument("--db", default="data/market_overview.duckdb")

    fetch = sub.add_parser("fetch", help="抓取数据并写入规范仓库")
    fetch.add_argument("--portfolio", help="组合 JSON 路径（可选）")
    fetch.add_argument("--db", default="data/market_overview.duckdb")
    fetch.add_argument("--data-dir", default="data/cache", help="原始缓存目录")
    fetch.add_argument("--window", type=int, default=120, help="统计窗口（交易日）")
    fetch.add_argument(
        "--kinds",
        default="industry,concept",
        help="板块类别，逗号分隔：industry,concept",
    )
    fetch.add_argument(
        "--focus-boards",
        type=int,
        default=5,
        help="按当日涨跌幅绝对值选取并抓取历史/成分的重点板块数",
    )
    fetch.add_argument(
        "--watch-boards",
        default="",
        help="额外强制跟踪的板块 thscode 或名称，逗号分隔",
    )
    fetch.add_argument(
        "--symbols",
        default="",
        help="额外观察的 6 位股票代码，逗号分隔",
    )
    fetch.add_argument("--skip-breadth", action="store_true")
    fetch.add_argument("--skip-special", action="store_true")

    report = sub.add_parser("report", help="从规范仓库渲染 Markdown 报告")
    report.add_argument("--portfolio", help="组合 JSON 路径（可选）")
    report.add_argument("--db", default="data/market_overview.duckdb")
    report.add_argument(
        "--out",
        default="data/reports/market_overview.md",
        help="报告输出路径",
    )

    status = sub.add_parser("status", help="查看本地规范仓库表行数")
    status.add_argument("--db", default="data/market_overview.duckdb")
    return parser


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = _parser().parse_args()
    portfolio = Portfolio.from_file(args.portfolio) if getattr(args, "portfolio", None) else None

    if args.command == "init":
        store = Store(args.db)
        store.init_schema()
        print(f"规范仓库已初始化: {Path(args.db)}")
    elif args.command == "fetch":
        if portfolio is not None and portfolio.window_days != args.window:
            print(
                f"注意：组合文件 window_days={portfolio.window_days}，"
                f"命令行 --window={args.window}，以组合文件为准"
            )
        window = portfolio.window_days if portfolio is not None else args.window
        kinds = tuple(part.strip() for part in args.kinds.split(",") if part.strip())
        symbols = tuple(
            part.strip() for part in (args.symbols or "").split(",") if part.strip()
        )
        watch_boards = tuple(
            part.strip() for part in (args.watch_boards or "").split(",") if part.strip()
        )
        summary = fetch_all(
            args.data_dir,
            args.db,
            portfolio,
            window_days=window,
            kinds=kinds,
            focus_boards=args.focus_boards,
            watch_boards=watch_boards,
            extra_symbols=symbols,
            include_breadth=not args.skip_breadth,
            include_special=not args.skip_special,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.command == "report":
        out = render(args.db, portfolio, args.out)
        print(f"报告已生成: {Path(out)}")
    elif args.command == "status":
        store = Store(args.db)
        for table in (
            "index_daily",
            "stock_daily",
            "stock_snapshot",
            "board_meta",
            "board_daily",
            "board_constituent_daily",
            "market_breadth_daily",
            "fetch_log",
        ):
            print(f"{table:28s} {store.table_count(table)}")
    else:
        raise SystemExit("未知命令")


if __name__ == "__main__":
    main()
