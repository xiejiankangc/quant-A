"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .fetch import fetch_all
from .portfolio import Portfolio
from .report import render


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market_overview")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="抓取数据并写入本地缓存")
    fetch.add_argument("--portfolio", required=True, help="组合 JSON 路径")
    fetch.add_argument("--data-dir", default="data/cache", help="缓存目录")
    fetch.add_argument("--window", type=int, default=60, help="统计窗口（交易日）")
    fetch.add_argument(
        "--kinds",
        default="industry,concept",
        help="板块类别，逗号分隔：industry,concept",
    )
    fetch.add_argument(
        "--sample-boards",
        type=int,
        default=3,
        help="每类板块抓取明细样例的板块数量",
    )

    report = sub.add_parser("report", help="从缓存渲染 Markdown 报告")
    report.add_argument("--portfolio", required=True, help="组合 JSON 路径")
    report.add_argument("--data-dir", default="data/cache", help="缓存目录")
    report.add_argument(
        "--out",
        default="data/reports/market_overview.md",
        help="报告输出路径",
    )
    return parser


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = _parser().parse_args()
    portfolio = Portfolio.from_file(args.portfolio)

    if args.command == "fetch":
        if portfolio.window_days != args.window:
            print(
                f"注意：组合文件 window_days={portfolio.window_days}，"
                f"命令行 --window={args.window}，以组合文件为准"
            )
        kinds = tuple(part.strip() for part in args.kinds.split(",") if part.strip())
        summary = fetch_all(
            args.data_dir,
            portfolio,
            kinds=kinds,
            sample_boards=args.sample_boards,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.command == "report":
        out = render(args.data_dir, portfolio, args.out)
        print(f"报告已生成: {Path(out)}")
    else:
        raise SystemExit("未知命令")

