"""市场全景辅助决策系统端到端自检脚本。

用法：
    python scripts/verify_market_overview.py \\
        --portfolio examples/portfolio.sample.json \\
        --db data/market_overview.duckdb \\
        --data-dir data/cache

脚本执行抓取、渲染报告并检查关键表是否落盘；任何数据源失败都会导致非零退出码。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_overview.fetch import fetch_all  # noqa: E402
from market_overview.portfolio import Portfolio  # noqa: E402
from market_overview.report import render  # noqa: E402
from market_overview.store import Store  # noqa: E402


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--portfolio")
    parser.add_argument("--db", default="data/market_overview.duckdb")
    parser.add_argument("--data-dir", default="data/cache")
    parser.add_argument("--window", type=int, default=120)
    parser.add_argument("--focus-boards", type=int, default=3)
    parser.add_argument("--kinds", default="industry,concept")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--watch-boards", default="")
    parser.add_argument("--out", default="data/reports/verify_report.md")
    parser.add_argument("--skip-breadth", action="store_true")
    parser.add_argument("--skip-special", action="store_true")
    args = parser.parse_args()

    portfolio = Portfolio.from_file(args.portfolio) if args.portfolio else None
    kinds = tuple(part.strip() for part in args.kinds.split(",") if part.strip())
    symbols = tuple(part.strip() for part in args.symbols.split(",") if part.strip())
    watch_boards = tuple(
        part.strip() for part in args.watch_boards.split(",") if part.strip()
    )

    print("[market_overview] 抓取并落库…")
    summary = fetch_all(
        args.data_dir,
        args.db,
        portfolio,
        window_days=portfolio.window_days if portfolio else args.window,
        kinds=kinds,
        focus_boards=args.focus_boards,
        watch_boards=watch_boards,
        extra_symbols=symbols,
        include_breadth=not args.skip_breadth,
        include_special=not args.skip_special,
    )
    print(f"  抓取项 {len(summary.get('artifacts', {}))}，失败项 {len(summary.get('errors', {}))}")

    print("[market_overview] 渲染报告…")
    out = render(args.db, portfolio, args.out)
    print(f"  报告 {out}")

    store = Store(args.db)
    required = {
        "index_daily": store.table_count("index_daily"),
        "stock_daily": store.table_count("stock_daily"),
        "board_daily": store.table_count("board_daily"),
        "board_meta": store.table_count("board_meta"),
    }
    print("[market_overview] 关键表行数")
    for table, count in required.items():
        print(f"  {table}: {count}")

    if summary.get("errors"):
        print("[market_overview] 自检失败：存在数据源错误")
        for key, message in summary["errors"].items():
            print(f"  {key}: {message}")
        raise SystemExit(1)
    if any(count <= 0 for count in required.values()):
        print("[market_overview] 自检失败：关键表为空")
        raise SystemExit(1)
    print("[market_overview] 自检通过")


if __name__ == "__main__":
    main()
