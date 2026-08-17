"""数据抓取层：同花顺 CLI（个股/基准）+ 东财板块（market_data 自适应通道）。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from market_data import em

from .cache import Cache
from .portfolio import Portfolio

TZ = ZoneInfo("Asia/Shanghai")


def _run_cli(args: list[str]) -> dict[str, Any]:
    exe = shutil.which("hithink-finance")
    if exe is None:
        raise RuntimeError("找不到 hithink-finance CLI，请先按 README 安装")
    if os.name == "nt":
        command = subprocess.list2cmdline([exe, *args])
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    else:
        proc = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"hithink-finance 退出码 {proc.returncode}: {proc.stderr.strip()[:300]}"
        )
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"hithink-finance 输出不是 JSON: {proc.stdout[:300]}") from exc
    if not envelope.get("ok"):
        error = envelope.get("error") or {}
        message = error.get("message") or envelope.get("message") or "未知错误"
        raise RuntimeError(f"hithink-finance 调用失败: {error.get('code')} {message}")
    return envelope.get("data") or {}


def window_ms(window_days: int) -> tuple[int, int]:
    """按交易窗口换算查询时间戳；日历回看取 2 倍窗口以覆盖停市日。"""
    end = datetime.now(TZ)
    start = end - timedelta(days=window_days * 2)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def fetch_index_history(thscode: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    data = _run_cli(
        [
            "--source",
            "remote",
            "index",
            "history",
            "--thscode",
            thscode,
            "--start-ms",
            str(start_ms),
            "--end-ms",
            str(end_ms),
            "--format",
            "json",
        ]
    )
    return data.get("item") or []


def fetch_stock_history(
    thscode: str,
    start_ms: int,
    end_ms: int,
    *,
    adjust: str = "forward",
) -> list[dict[str, Any]]:
    data = _run_cli(
        [
            "--source",
            "remote",
            "market",
            "history",
            "--thscode",
            thscode,
            "--start-ms",
            str(start_ms),
            "--end-ms",
            str(end_ms),
            "--adjust",
            adjust,
            "--format",
            "json",
        ]
    )
    return data.get("item") or []


def fetch_all(
    cache_root: str | Path,
    portfolio: Portfolio,
    *,
    kinds: tuple[str, ...] = ("industry", "concept"),
    sample_boards: int = 3,
) -> dict[str, Any]:
    cache = Cache(cache_root)
    fetched_at = datetime.now(TZ).isoformat(timespec="seconds")
    start_ms, end_ms = window_ms(portfolio.window_days)
    start_date = datetime.fromtimestamp(start_ms / 1000, TZ).date().isoformat()
    end_date = datetime.fromtimestamp(end_ms / 1000, TZ).date().isoformat()
    summary: dict[str, Any] = {
        "fetched_at": fetched_at,
        "window_days": portfolio.window_days,
        "start_date": start_date,
        "end_date": end_date,
        "artifacts": {},
        "errors": {},
    }

    benchmark = fetch_index_history(portfolio.benchmark, start_ms, end_ms)
    cache.write_rows(
        "benchmark/index_history.jsonl",
        benchmark,
        meta={
            "thscode": portfolio.benchmark,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "fetched_at": fetched_at,
        },
    )
    summary["artifacts"]["benchmark"] = {
        "path": "benchmark/index_history.jsonl",
        "rows": len(benchmark),
    }

    for kind in kinds:
        if kind not in ("industry", "concept"):
            raise ValueError(f"kinds 只支持 industry/concept，收到: {kind!r}")
        boards = em.boards(kind)
        cache.write_rows(
            f"boards/{kind}.jsonl",
            boards,
            meta={"kind": kind, "fetched_at": fetched_at},
        )
        summary["artifacts"][f"boards_{kind}"] = {
            "path": f"boards/{kind}.jsonl",
            "rows": len(boards),
        }

        ranked = sorted(
            boards,
            key=lambda row: abs(row.get("change_pct") or 0.0),
            reverse=True,
        )[:sample_boards]
        for board in ranked:
            code = board["code"]
            board_name = board.get("name") or code
            try:
                history = em.board_history(code, start_date, end_date)
                cache.write_rows(
                    f"boards/{kind}/{code}/history.jsonl",
                    history,
                    meta={
                        "code": code,
                        "name": board_name,
                        "start_date": start_date,
                        "end_date": end_date,
                        "fetched_at": fetched_at,
                    },
                )
                summary["artifacts"][f"boards_{kind}_{code}_history"] = {
                    "path": f"boards/{kind}/{code}/history.jsonl",
                    "rows": len(history),
                }
            except Exception as exc:  # noqa: BLE001 - 汇总错误后继续
                summary["errors"][f"boards/{kind}/{code}/history"] = (
                    f"{type(exc).__name__}: {exc}"
                )

            try:
                constituents = em.board_constituents(code)
                cache.write_rows(
                    f"boards/{kind}/{code}/constituents.jsonl",
                    constituents,
                    meta={
                        "code": code,
                        "name": board_name,
                        "fetched_at": fetched_at,
                    },
                )
                summary["artifacts"][f"boards_{kind}_{code}_constituents"] = {
                    "path": f"boards/{kind}/{code}/constituents.jsonl",
                    "rows": len(constituents),
                }
            except Exception as exc:  # noqa: BLE001
                summary["errors"][f"boards/{kind}/{code}/constituents"] = (
                    f"{type(exc).__name__}: {exc}"
                )

    for holding in portfolio.holdings:
        history = fetch_stock_history(holding.thscode, start_ms, end_ms)
        cache.write_rows(
            f"stocks/{holding.symbol}/history.jsonl",
            history,
            meta={
                "symbol": holding.symbol,
                "thscode": holding.thscode,
                "adjust": "forward",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "fetched_at": fetched_at,
            },
        )
        summary["artifacts"][f"stocks_{holding.symbol}_history"] = {
            "path": f"stocks/{holding.symbol}/history.jsonl",
            "rows": len(history),
        }

        profile = em.stock_profile(holding.symbol)
        cache.write_rows(
            f"stocks/{holding.symbol}/profile.jsonl",
            [profile],
            meta={"symbol": holding.symbol, "fetched_at": fetched_at},
        )
        summary["artifacts"][f"stocks_{holding.symbol}_profile"] = {
            "path": f"stocks/{holding.symbol}/profile.jsonl",
            "rows": 1,
        }

    cache.write_json("fetch_summary.json", summary)
    return summary
