"""数据源适配层：同花顺 CLI + 东财补充信息。

所有 CLI 调用统一在这里完成，业务代码不直接拼接命令行；东财个股补充信息
复用 `market_data.em`，东财不可用时由 fetch 层选择腾讯历史通道。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from market_data import em


TZ = ZoneInfo("Asia/Shanghai")


def _cli_path() -> str:
    """定位 hithink-finance CLI，兼容 PATH 未包含 npm 全局目录的情况。"""
    found = shutil.which("hithink-finance")
    if found:
        return found
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidate = Path(appdata) / "npm" / "hithink-finance.cmd"
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("找不到 hithink-finance CLI，请先按 README 安装")


def run_cli(args: list[str], *, timeout: int = 240) -> dict[str, Any]:
    """运行 hithink-finance CLI，解析 JSON envelope，失败抛出 RuntimeError。"""
    exe = _cli_path()
    if os.name == "nt":
        command = subprocess.list2cmdline([exe, *[str(item) for item in args]])
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    else:
        proc = subprocess.run(
            [exe, *[str(item) for item in args]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"hithink-finance 退出码 {proc.returncode}: {proc.stderr.strip()[:300]}"
        )
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"hithink-finance 输出不是 JSON: {proc.stdout[:300]}"
        ) from exc
    if not envelope.get("ok"):
        error = envelope.get("error") or {}
        message = error.get("message") or envelope.get("message") or "未知错误"
        raise RuntimeError(
            f"hithink-finance 调用失败: {error.get('code')} {message}"
        )
    return envelope.get("data") or {}


def index_catalog(kind: str) -> list[dict[str, Any]]:
    """取同花顺行业/概念指数目录。

    kind 只接受 industry / concept，对应 CLI 的 industry / cn_concept。
    """
    tag = {"industry": "industry", "concept": "cn_concept"}.get(kind)
    if tag is None:
        raise ValueError(f"kind 只支持 industry/concept，收到: {kind!r}")
    data = run_cli(["index", "catalog", "--tag", tag, "--format", "json"])
    return data.get("item") or []


def index_snapshot(thscodes: list[str], *, chunk_size: int = 200) -> list[dict[str, Any]]:
    """批量取指数快照；按 chunk 拆批避免单次请求过大。"""
    codes = [str(code) for code in thscodes if str(code).strip()]
    if not codes:
        return []
    rows: list[dict[str, Any]] = []
    for start in range(0, len(codes), chunk_size):
        chunk = codes[start : start + chunk_size]
        data = run_cli(
            ["index", "snapshot", "--thscodes", ",".join(chunk), "--format", "json"]
        )
        rows.extend(data.get("item") or [])
    return rows


def index_history(thscode: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    data = run_cli(
        [
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


def index_constituents(thscode: str) -> list[dict[str, Any]]:
    data = run_cli(
        ["index", "constituents", "--thscode", thscode, "--format", "json"]
    )
    return data.get("item") or []


def market_snapshot_all(
    *,
    limit: int = 100,
    max_pages: int = 100,
) -> list[dict[str, Any]]:
    """分页取全市场 A 股快照。"""
    rows: list[dict[str, Any]] = []
    total: int | None = None
    for page in range(max_pages):
        offset = page * limit
        data = run_cli(
            [
                "market",
                "snapshot",
                "--limit",
                str(limit),
                "--offset",
                str(offset),
                "--format",
                "json",
            ]
        )
        item = data.get("item") or []
        if total is None:
            total = int(data.get("total") or len(item))
        rows.extend(item)
        if not item or len(rows) >= total:
            break
    return rows


def special_pool(kind: str, *, size: int = 200) -> list[dict[str, Any]]:
    """取涨停/跌停/炸板池全部条目。"""
    command = {
        "limit_up": ["special", "limit-up-pool"],
        "limit_down": ["special", "limit-down-pool"],
        "limit_break": ["special", "limit-break-pool"],
    }.get(kind)
    if command is None:
        raise ValueError(f"kind 只支持 limit_up/limit_down/limit_break，收到: {kind!r}")

    rows: list[dict[str, Any]] = []
    total: int | None = None
    for page in range(1, 100):
        data = run_cli(
            command
            + [
                "--size",
                str(size),
                "--page",
                str(page),
                "--format",
                "json",
            ]
        )
        item = data.get("item") or []
        pagination = data.get("pagination") or {}
        if total is None:
            total = int(pagination.get("total") or len(item))
        rows.extend(item)
        if not item or len(rows) >= total:
            break
    return rows


def ladder_height() -> int | None:
    """从连板天梯的最近一个交易日取最高连板数。"""
    data = run_cli(["special", "limit-up-ladder", "--format", "json"])
    items = data.get("item") or []
    if not items:
        return None
    latest = items[0]
    boards = latest.get("boards") or {}
    heights: list[int] = []
    for group in boards.values():
        for row in group or []:
            value = row.get("board_num")
            if isinstance(value, (int, float)):
                heights.append(int(value))
    return max(heights) if heights else None


def stock_snapshot(thscodes: list[str]) -> list[dict[str, Any]]:
    """取个股普通行情快照；用于板块成分股的当日价格与涨跌幅。"""
    codes = [str(code) for code in thscodes if str(code).strip()]
    if not codes:
        return []
    rows: list[dict[str, Any]] = []
    for start in range(0, len(codes), 200):
        chunk = codes[start : start + 200]
        data = run_cli(
            ["market", "snapshot", "--thscodes", ",".join(chunk), "--format", "json"]
        )
        rows.extend(data.get("item") or [])
    return rows


def stock_profile(symbol: str) -> dict[str, Any]:
    """东财个股补充信息：市值、股本与换手率。"""
    return em.stock_profile(symbol)


def now_text() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def date_text(ts_ms: int | None) -> str | None:
    if not ts_ms:
        return None
    return datetime.fromtimestamp(int(ts_ms) / 1000, TZ).date().isoformat()
