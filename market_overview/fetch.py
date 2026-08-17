"""数据抓取编排：远端取数 -> 原始缓存 -> DuckDB 规范仓库。

数据路由：
- 板块目录/快照/成分：东财行业+概念为默认主源，同花顺行业+概念保留为对照；
- 板块多日历史：同花顺指数历史为主；东财原生日线受 push2his 风控限制，
  多日指标改用「每日东财板块快照累计」自建序列，`--em-history` 可选尝试原生日线；
- 指数目录/历史、全市场快照、特色数据：同花顺 CLI；
- 个股历史：腾讯（akshare）为主，来源字段如实标注；
- 个股市值/股本/换手率补充：东财 `market_data.em`。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from market_data import tx

from .cache import Cache
from .portfolio import Holding, Portfolio
from .sources import (
    date_text,
    em_board_constituents,
    em_board_history,
    em_boards,
    index_catalog,
    index_constituents,
    index_history,
    index_snapshot,
    ladder_height,
    market_snapshot_all,
    now_text,
    special_pool,
    stock_profile,
    stock_snapshot,
)
from .store import Store


TZ = ZoneInfo("Asia/Shanghai")


def window_range(window_days: int) -> tuple[str, str, int, int]:
    """日历回看取 2 倍窗口覆盖停市日。"""
    end = datetime.now(TZ)
    start = end - timedelta(days=window_days * 2)
    return (
        start.date().isoformat(),
        end.date().isoformat(),
        int(start.timestamp() * 1000),
        int(end.timestamp() * 1000),
    )


def fetch_all(
    cache_root: str | Path,
    store_path: str | Path,
    portfolio: Portfolio | None,
    *,
    window_days: int = 120,
    kinds: tuple[str, ...] = ("industry", "concept"),
    focus_boards: int = 5,
    watch_boards: tuple[str, ...] = (),
    extra_symbols: tuple[str, ...] = (),
    include_breadth: bool = True,
    include_special: bool = True,
    em_history: bool = False,
) -> dict[str, Any]:
    cache = Cache(cache_root)
    store = Store(store_path)
    store.init_schema()

    fetched_at = now_text()
    start_date, end_date, start_ms, end_ms = window_range(window_days)
    summary: dict[str, Any] = {
        "fetched_at": fetched_at,
        "window_days": window_days,
        "start_date": start_date,
        "end_date": end_date,
        "artifacts": {},
        "errors": {},
        "warnings": {},
        "notes": {},
    }

    # 市场基准固定上证指数；组合基准默认沪深 300，另存供 β 使用。
    benchmarks = ["000001.SH"]
    if portfolio is not None and portfolio.benchmark not in benchmarks:
        benchmarks.append(portfolio.benchmark)
    for thscode in benchmarks:
        try:
            rows = index_history(thscode, start_ms, end_ms)
            normalized = [
                _index_row(row, thscode, fetched_at)
                for row in rows
                if row.get("date_ms")
            ]
            store.write_rows(
                "index_daily",
                normalized,
                columns=[
                    "trade_date",
                    "thscode",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "amount",
                    "source",
                    "as_of",
                    "ingest_time",
                ],
            )
            cache.write_rows(
                f"benchmark/{thscode.replace('.', '_')}.jsonl",
                rows,
                meta={"thscode": thscode, "fetched_at": fetched_at},
            )
            store.log(
                f"index_history:{thscode}",
                "ok",
                rows=len(normalized),
                fetched_at=fetched_at,
            )
            summary["artifacts"][f"index_{thscode}"] = {
                "path": f"benchmark/{thscode.replace('.', '_')}.jsonl",
                "rows": len(normalized),
            }
        except Exception as exc:  # noqa: BLE001 - 单源失败不阻断整体
            _record_error(store, summary, f"index_history:{thscode}", exc, fetched_at)

    watch_symbols = _watch_symbols(portfolio, extra_symbols)
    prices: dict[str, float] = {}

    if watch_symbols:
        _fetch_stock_histories(
            cache,
            store,
            summary,
            watch_symbols,
            start_date,
            end_date,
            fetched_at,
            prices,
        )
        _fetch_stock_profiles(
            cache,
            store,
            summary,
            watch_symbols,
            fetched_at,
            prices,
        )

    for kind in kinds:
        if kind not in ("industry", "concept"):
            raise ValueError(f"kinds 只支持 industry/concept，收到: {kind!r}")
        _fetch_eastmoney_boards(
            cache,
            store,
            summary,
            kind,
            start_date,
            end_date,
            fetched_at,
            focus_boards=focus_boards,
            watch_boards=watch_boards,
            em_history=em_history,
        )
        _fetch_ths_boards(
            cache,
            store,
            summary,
            kind,
            start_ms,
            end_ms,
            fetched_at,
            focus_boards=focus_boards,
            watch_boards=watch_boards,
        )

    if include_breadth:
        _fetch_breadth(cache, store, summary, fetched_at)
    if include_special:
        _fetch_special(cache, store, summary, fetched_at)

    cache.write_json("fetch_summary.json", summary)
    return summary


def _watch_symbols(
    portfolio: Portfolio | None,
    extra_symbols: tuple[str, ...],
) -> list[str]:
    symbols: list[str] = []
    if portfolio is not None:
        symbols.extend(holding.symbol for holding in portfolio.holdings)
    symbols.extend(str(symbol).strip() for symbol in extra_symbols)
    seen: set[str] = set()
    unique: list[str] = []
    for symbol in symbols:
        if symbol and symbol not in seen:
            seen.add(symbol)
            unique.append(symbol)
    return unique


def _fetch_stock_histories(
    cache: Cache,
    store: Store,
    summary: dict[str, Any],
    symbols: list[str],
    start_date: str,
    end_date: str,
    fetched_at: str,
    prices: dict[str, float],
) -> None:
    def one(symbol: str) -> tuple[str, list[dict[str, Any]]]:
        return symbol, tx.daily_history(symbol, start_date, end_date, adjust="qfq")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(one, symbol) for symbol in symbols]
        for future in as_completed(futures):
            try:
                symbol, rows = future.result()
                normalized = [
                    _stock_row(row, fetched_at)
                    for row in rows
                    if row.get("date")
                ]
                store.write_rows(
                    "stock_daily",
                    normalized,
                    columns=[
                        "trade_date",
                        "symbol",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "turnover_rate_pct",
                        "amount",
                        "source",
                        "adjust",
                        "as_of",
                        "ingest_time",
                    ],
                )
                cache.write_rows(
                    f"stocks/{symbol}/history.jsonl",
                    rows,
                    meta={
                        "symbol": symbol,
                        "source": "tencent",
                        "adjust": "qfq",
                        "fetched_at": fetched_at,
                    },
                )
                store.log(
                    f"stock_history:{symbol}",
                    "ok",
                    rows=len(normalized),
                    fetched_at=fetched_at,
                )
                summary["artifacts"][f"stock_history_{symbol}"] = {
                    "path": f"stocks/{symbol}/history.jsonl",
                    "rows": len(normalized),
                }
                if normalized:
                    prices[symbol] = float(normalized[-1]["close"])
            except Exception as exc:  # noqa: BLE001
                _record_error(store, summary, f"stock_history:{symbol}", exc, fetched_at)


def _fetch_stock_profiles(
    cache: Cache,
    store: Store,
    summary: dict[str, Any],
    symbols: list[str],
    fetched_at: str,
    prices: dict[str, float],
) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(stock_profile, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                profile = future.result()
                rows = [_profile_row(profile, symbol, fetched_at)]
                store.write_rows(
                    "stock_snapshot",
                    rows,
                    columns=[
                        "as_of",
                        "symbol",
                        "name",
                        "last_price",
                        "change_pct",
                        "volume",
                        "amount",
                        "turnover_rate_pct",
                        "total_market_cap",
                        "float_market_cap",
                        "total_shares",
                        "float_shares",
                        "delayed",
                        "source",
                        "ingest_time",
                    ],
                )
                cache.write_rows(
                    f"stocks/{symbol}/profile.jsonl",
                    [profile],
                    meta={"symbol": symbol, "fetched_at": fetched_at},
                )
                store.log(
                    f"stock_profile:{symbol}",
                    "ok",
                    rows=1,
                    fetched_at=fetched_at,
                )
                summary["artifacts"][f"stock_profile_{symbol}"] = {
                    "path": f"stocks/{symbol}/profile.jsonl",
                    "rows": 1,
                }
                if profile.get("price") is not None:
                    prices[symbol] = float(profile["price"])
            except Exception as exc:  # noqa: BLE001
                _record_error(store, summary, f"stock_profile:{symbol}", exc, fetched_at)


def _fetch_eastmoney_boards(
    cache: Cache,
    store: Store,
    summary: dict[str, Any],
    kind: str,
    start_date: str,
    end_date: str,
    fetched_at: str,
    *,
    focus_boards: int,
    watch_boards: tuple[str, ...],
    em_history: bool,
) -> None:
    """东财行业/概念板块：目录 + 全量每日快照 + 重点板块成分（默认主源）。"""
    try:
        boards = em_boards(kind)
    except Exception as exc:  # noqa: BLE001
        _record_error(
            store, summary, f"eastmoney_board_catalog:{kind}", exc, fetched_at
        )
        return

    meta_rows = [
        {
            "thscode": _em_board_code(row["code"]),
            "kind": kind,
            "name": row.get("name"),
            "source": "eastmoney-board",
            "ingest_time": fetched_at,
        }
        for row in boards
        if row.get("code")
    ]
    store.write_rows(
        "board_meta",
        meta_rows,
        columns=["thscode", "kind", "name", "source", "ingest_time"],
    )
    cache.write_rows(
        f"boards_em/{kind}/meta.jsonl",
        boards,
        meta={"kind": kind, "provider": "eastmoney", "fetched_at": fetched_at},
    )
    store.log(
        f"eastmoney_board_catalog:{kind}",
        "ok",
        rows=len(meta_rows),
        fetched_at=fetched_at,
    )
    summary["artifacts"][f"eastmoney_board_catalog_{kind}"] = {
        "path": f"boards_em/{kind}/meta.jsonl",
        "rows": len(meta_rows),
    }

    as_of = datetime.now(TZ).date().isoformat()
    snapshot_rows = [
        _em_board_snapshot_row(row, kind, as_of, fetched_at)
        for row in boards
        if row.get("code")
    ]
    store.write_rows(
        "board_daily",
        snapshot_rows,
        columns=[
            "trade_date",
            "thscode",
            "kind",
            "close",
            "change_pct",
            "volume",
            "amount",
            "turnover_rate_pct",
            "source",
            "as_of",
            "ingest_time",
        ],
    )
    cache.write_rows(
        f"boards_em/{kind}/snapshot.jsonl",
        boards,
        meta={
            "kind": kind,
            "provider": "eastmoney",
            "as_of": as_of,
            "fetched_at": fetched_at,
        },
    )
    store.log(
        f"eastmoney_board_snapshot:{kind}",
        "ok",
        rows=len(snapshot_rows),
        fetched_at=fetched_at,
    )
    summary["artifacts"][f"eastmoney_board_snapshot_{kind}"] = {
        "path": f"boards_em/{kind}/snapshot.jsonl",
        "rows": len(snapshot_rows),
    }

    depth = _snapshot_depth(store, kind, "eastmoney-board-snapshot")
    summary["notes"][f"eastmoney_{kind}_snapshot_depth"] = depth
    if not em_history:
        summary["notes"]["eastmoney_board_history"] = (
            "未启用 --em-history；东财板块多日指标由每日快照累计构建，"
            "原生日线历史接口在部分网络环境受风控"
        )

    focus = _select_em_focus_boards(boards, focus_boards, watch_boards)
    for row in focus:
        code = str(row["code"])
        _fetch_em_board_detail(
            cache,
            store,
            summary,
            kind,
            code,
            str(row.get("name") or code),
            start_date,
            end_date,
            fetched_at,
            em_history=em_history,
        )


def _em_board_code(code: str) -> str:
    """东财板块代码统一命名空间：EM:BKxxxx，与同花顺 thscode 区分。"""
    value = str(code).strip().upper()
    if value.startswith("90."):
        value = value[3:]
    if value.startswith("EM:"):
        return value
    if not value.startswith("BK"):
        value = f"BK{value}"
    return f"EM:{value}"


def _select_em_focus_boards(
    boards: list[dict[str, Any]],
    focus_boards: int,
    watch_boards: tuple[str, ...],
) -> list[dict[str, Any]]:
    """按当日涨跌幅绝对值取东财 Top N，并追加用户指定的代码/名称。"""
    wanted: set[str] = set()
    for token in watch_boards:
        token = token.strip()
        if not token:
            continue
        for row in boards:
            code = str(row.get("code") or "")
            name = str(row.get("name") or "")
            if token in (code, f"EM:{code}", name):
                wanted.add(code)
                break

    top = sorted(
        boards,
        key=lambda row: abs(_num(row.get("change_pct")) or 0.0),
        reverse=True,
    )[: max(0, focus_boards)]
    selected = top + [row for row in boards if row.get("code") in wanted]
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in selected:
        code = str(row.get("code"))
        if code and code not in seen:
            seen.add(code)
            result.append(row)
    return result


def _fetch_em_board_detail(
    cache: Cache,
    store: Store,
    summary: dict[str, Any],
    kind: str,
    code: str,
    name: str,
    start_date: str,
    end_date: str,
    fetched_at: str,
    *,
    em_history: bool,
) -> None:
    board_code = _em_board_code(code)

    if em_history:
        try:
            history = em_board_history(code, start_date, end_date)
            normalized = [
                _em_board_history_row(row, kind, board_code, fetched_at)
                for row in history
                if row.get("date")
            ]
            store.write_rows(
                "board_daily",
                normalized,
                columns=[
                    "trade_date",
                    "thscode",
                    "kind",
                    "close",
                    "change_pct",
                    "volume",
                    "amount",
                    "turnover_rate_pct",
                    "source",
                    "as_of",
                    "ingest_time",
                ],
            )
            cache.write_rows(
                f"boards_em/{kind}/{code}/history.jsonl",
                history,
                meta={
                    "code": code,
                    "name": name,
                    "fetched_at": fetched_at,
                },
            )
            store.log(
                f"eastmoney_board_history:{code}",
                "ok",
                rows=len(normalized),
                fetched_at=fetched_at,
            )
            summary["artifacts"][f"eastmoney_board_history_{code}"] = {
                "path": f"boards_em/{kind}/{code}/history.jsonl",
                "rows": len(normalized),
            }
        except Exception as exc:  # noqa: BLE001 - 原生日线属可选增强，失败降级为警告
            _record_warning(
                store, summary, f"eastmoney_board_history:{code}", exc, fetched_at
            )

    try:
        members = em_board_constituents(code)
        as_of = datetime.now(TZ).date().isoformat()
        rows = [
            {
                "as_of": as_of,
                "board_thscode": board_code,
                "symbol": str(member["symbol"]),
                "name": member.get("name"),
                "last_price": _num(member.get("price")),
                "change_pct": (
                    _num(member.get("change_pct")) / 100
                    if _num(member.get("change_pct")) is not None
                    else None
                ),
                "volume": _num(member.get("volume")),
                "amount": _num(member.get("amount")),
                "turnover_rate_pct": _num(member.get("turnover_rate")),
                "total_market_cap": _num(member.get("total_market_cap")),
                "float_market_cap": _num(member.get("float_market_cap")),
                "source": "eastmoney",
                "ingest_time": fetched_at,
            }
            for member in members
            if member.get("symbol")
        ]
        store.write_rows(
            "board_constituent_daily",
            rows,
            columns=[
                "as_of",
                "board_thscode",
                "symbol",
                "name",
                "last_price",
                "change_pct",
                "volume",
                "amount",
                "turnover_rate_pct",
                "total_market_cap",
                "float_market_cap",
                "source",
                "ingest_time",
            ],
        )
        cache.write_rows(
            f"boards_em/{kind}/{code}/constituents.jsonl",
            members,
            meta={
                "code": code,
                "name": name,
                "fetched_at": fetched_at,
            },
        )
        store.log(
            f"eastmoney_board_constituents:{code}",
            "ok",
            rows=len(rows),
            fetched_at=fetched_at,
        )
        summary["artifacts"][f"eastmoney_board_constituents_{code}"] = {
            "path": f"boards_em/{kind}/{code}/constituents.jsonl",
            "rows": len(rows),
        }
    except Exception as exc:  # noqa: BLE001
        _record_error(
            store, summary, f"eastmoney_board_constituents:{code}", exc, fetched_at
        )


def _fetch_ths_boards(
    cache: Cache,
    store: Store,
    summary: dict[str, Any],
    kind: str,
    start_ms: int,
    end_ms: int,
    fetched_at: str,
    *,
    focus_boards: int,
    watch_boards: tuple[str, ...],
) -> None:
    try:
        catalog = index_catalog(kind)
        store.write_rows(
            "board_meta",
            [
                {
                    "thscode": str(row["thscode"]),
                    "kind": kind,
                    "name": row.get("name"),
                    "source": "hithink-index-catalog",
                    "ingest_time": fetched_at,
                }
                for row in catalog
                if row.get("thscode")
            ],
            columns=["thscode", "kind", "name", "source", "ingest_time"],
        )
        cache.write_rows(
            f"boards/{kind}/meta.jsonl",
            catalog,
            meta={"kind": kind, "fetched_at": fetched_at},
        )
        store.log(
            f"board_catalog:{kind}",
            "ok",
            rows=len(catalog),
            fetched_at=fetched_at,
        )
        summary["artifacts"][f"board_catalog_{kind}"] = {
            "path": f"boards/{kind}/meta.jsonl",
            "rows": len(catalog),
        }
    except Exception as exc:  # noqa: BLE001
        _record_error(store, summary, f"board_catalog:{kind}", exc, fetched_at)
        return

    codes = [str(row["thscode"]) for row in catalog if row.get("thscode")]
    try:
        snapshots = index_snapshot(codes)
        as_of = datetime.now(TZ).date().isoformat()
        normalized = [
            _board_snapshot_row(row, kind, as_of, fetched_at)
            for row in snapshots
            if row.get("thscode")
        ]
        store.write_rows(
            "board_daily",
            normalized,
            columns=[
                "trade_date",
                "thscode",
                "kind",
                "close",
                "change_pct",
                "volume",
                "amount",
                "turnover_rate_pct",
                "source",
                "as_of",
                "ingest_time",
            ],
        )
        cache.write_rows(
            f"boards/{kind}/snapshot.jsonl",
            snapshots,
            meta={"kind": kind, "as_of": as_of, "fetched_at": fetched_at},
        )
        store.log(
            f"board_snapshot:{kind}",
            "ok",
            rows=len(normalized),
            fetched_at=fetched_at,
        )
        summary["artifacts"][f"board_snapshot_{kind}"] = {
            "path": f"boards/{kind}/snapshot.jsonl",
            "rows": len(normalized),
        }
    except Exception as exc:  # noqa: BLE001
        _record_error(store, summary, f"board_snapshot:{kind}", exc, fetched_at)
        return

    ranked = _select_focus_boards(
        snapshots, catalog, focus_boards, watch_boards
    )
    for row in ranked:
        code = str(row.get("thscode"))
        name = _board_name(catalog, code)
        _fetch_board_detail(
            cache,
            store,
            summary,
            kind,
            code,
            name,
            start_ms,
            end_ms,
            fetched_at,
        )


def _select_focus_boards(
    snapshots: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    focus_boards: int,
    watch_boards: tuple[str, ...],
) -> list[dict[str, Any]]:
    """按涨跌幅绝对值取 Top N，并追加用户指定代码/名称的板块。"""
    snapshot_by_code = {str(row.get("thscode")): row for row in snapshots}
    wanted_codes: set[str] = set()
    for token in watch_boards:
        token = token.strip()
        if not token:
            continue
        for row in catalog:
            if str(row.get("thscode")) == token or str(row.get("name")) == token:
                wanted_codes.add(str(row["thscode"]))
                break

    top = sorted(
        snapshots,
        key=lambda row: abs(_num(row.get("price_change_ratio_pct")) or 0.0),
        reverse=True,
    )[: max(0, focus_boards)]
    selected = top + [snapshot_by_code[code] for code in wanted_codes if code in snapshot_by_code]
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in selected:
        code = str(row.get("thscode"))
        if code not in seen:
            seen.add(code)
            result.append(row)
    return result


def _fetch_board_detail(
    cache: Cache,
    store: Store,
    summary: dict[str, Any],
    kind: str,
    code: str,
    name: str,
    start_ms: int,
    end_ms: int,
    fetched_at: str,
) -> None:
    try:
        history = index_history(code, start_ms, end_ms)
        normalized = [
            _board_history_row(row, kind, code, fetched_at)
            for row in history
            if row.get("date_ms")
        ]
        store.write_rows(
            "board_daily",
            normalized,
            columns=[
                "trade_date",
                "thscode",
                "kind",
                "close",
                "change_pct",
                "volume",
                "amount",
                "turnover_rate_pct",
                "source",
                "as_of",
                "ingest_time",
            ],
        )
        cache.write_rows(
            f"boards/{kind}/{code}/history.jsonl",
            history,
            meta={
                "thscode": code,
                "name": name,
                "fetched_at": fetched_at,
            },
        )
        store.log(
            f"board_history:{code}",
            "ok",
            rows=len(normalized),
            fetched_at=fetched_at,
        )
        summary["artifacts"][f"board_history_{code}"] = {
            "path": f"boards/{kind}/{code}/history.jsonl",
            "rows": len(normalized),
        }
    except Exception as exc:  # noqa: BLE001
        _record_error(store, summary, f"board_history:{code}", exc, fetched_at)

    try:
        constituents = index_constituents(code)
        member_codes = [
            str(row["thscode"]) for row in constituents if row.get("thscode")
        ]
        member_names = {
            str(row["thscode"]): row.get("name") or row.get("ticker") or ""
            for row in constituents
            if row.get("thscode")
        }
        quotes = {
            str(row["thscode"]): row
            for row in stock_snapshot(member_codes)
            if row.get("thscode")
        }
        profiles = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(stock_profile, thscode[:6]): thscode
                for thscode in member_codes
            }
            for future in as_completed(futures):
                thscode = futures[future]
                try:
                    profiles[thscode] = future.result()
                except Exception:  # noqa: BLE001 - 成员缺失不阻断整个板块
                    profiles[thscode] = {}

        rows = []
        for thscode in member_codes:
            quote = quotes.get(thscode, {})
            profile = profiles.get(thscode, {})
            rows.append(
                {
                    "as_of": datetime.now(TZ).date().isoformat(),
                    "board_thscode": code,
                    "symbol": thscode[:6],
                    "name": member_names.get(thscode),
                    "last_price": _num(quote.get("last_price")) or _num(
                        profile.get("price")
                    ),
                    "change_pct": (
                        _num(quote.get("price_change_ratio_pct")) / 100
                        if _num(quote.get("price_change_ratio_pct")) is not None
                        else None
                    ),
                    "volume": _num(quote.get("volume")),
                    "amount": _num(quote.get("turnover")),
                    "turnover_rate_pct": _num(profile.get("turnover_rate")),
                    "total_market_cap": _num(profile.get("total_market_cap")),
                    "float_market_cap": _num(profile.get("float_market_cap")),
                    "source": "hithink+em",
                    "ingest_time": fetched_at,
                }
            )
        store.write_rows(
            "board_constituent_daily",
            rows,
            columns=[
                "as_of",
                "board_thscode",
                "symbol",
                "name",
                "last_price",
                "change_pct",
                "volume",
                "amount",
                "turnover_rate_pct",
                "total_market_cap",
                "float_market_cap",
                "source",
                "ingest_time",
            ],
        )
        cache.write_rows(
            f"boards/{kind}/{code}/constituents.jsonl",
            rows,
            meta={
                "thscode": code,
                "name": name,
                "fetched_at": fetched_at,
            },
        )
        store.log(
            f"board_constituents:{code}",
            "ok",
            rows=len(rows),
            fetched_at=fetched_at,
        )
        summary["artifacts"][f"board_constituents_{code}"] = {
            "path": f"boards/{kind}/{code}/constituents.jsonl",
            "rows": len(rows),
        }
    except Exception as exc:  # noqa: BLE001
        _record_error(store, summary, f"board_constituents:{code}", exc, fetched_at)


def _fetch_breadth(
    cache: Cache,
    store: Store,
    summary: dict[str, Any],
    fetched_at: str,
) -> None:
    try:
        rows = market_snapshot_all()
        up = down = flat = 0
        for row in rows:
            change = _num(row.get("price_change_ratio_pct"))
            if change is None or change == 0:
                flat += 1
            elif change > 0:
                up += 1
            else:
                down += 1
        as_of = datetime.now(TZ).date().isoformat()
        store.write_rows(
            "market_breadth_daily",
            [
                {
                    "as_of": as_of,
                    "total": len(rows),
                    "up_count": up,
                    "down_count": down,
                    "flat_count": flat,
                    "limit_up_count": None,
                    "limit_down_count": None,
                    "limit_break_count": None,
                    "ladder_height": None,
                    "source": "hithink-market-snapshot",
                    "ingest_time": fetched_at,
                }
            ],
            columns=[
                "as_of",
                "total",
                "up_count",
                "down_count",
                "flat_count",
                "limit_up_count",
                "limit_down_count",
                "limit_break_count",
                "ladder_height",
                "source",
                "ingest_time",
            ],
        )
        cache.write_rows(
            "market/snapshot.jsonl",
            rows,
            meta={"as_of": as_of, "fetched_at": fetched_at},
        )
        store.log(
            "market_breadth",
            "ok",
            rows=1,
            fetched_at=fetched_at,
        )
        summary["artifacts"]["market_breadth"] = {
            "path": "market/snapshot.jsonl",
            "rows": len(rows),
        }
    except Exception as exc:  # noqa: BLE001
        _record_error(store, summary, "market_breadth", exc, fetched_at)


def _fetch_special(
    cache: Cache,
    store: Store,
    summary: dict[str, Any],
    fetched_at: str,
) -> None:
    counts: dict[str, int | None] = {}
    for kind in ("limit_up", "limit_down", "limit_break"):
        try:
            rows = special_pool(kind)
            counts[kind] = len(rows)
            cache.write_rows(
                f"special/{kind}.jsonl",
                rows,
                meta={"kind": kind, "fetched_at": fetched_at},
            )
            store.log(
                f"special:{kind}",
                "ok",
                rows=len(rows),
                fetched_at=fetched_at,
            )
            summary["artifacts"][f"special_{kind}"] = {
                "path": f"special/{kind}.jsonl",
                "rows": len(rows),
            }
        except Exception as exc:  # noqa: BLE001
            counts[kind] = None
            _record_error(store, summary, f"special:{kind}", exc, fetched_at)

    try:
        height = ladder_height()
        ladder_rows = _ladder_rows(height)
        cache.write_rows(
            "special/ladder.jsonl",
            ladder_rows,
            meta={"fetched_at": fetched_at},
        )
        store.log(
            "special:ladder",
            "ok",
            rows=len(ladder_rows),
            fetched_at=fetched_at,
        )
        summary["artifacts"]["special_ladder"] = {
            "path": "special/ladder.jsonl",
            "rows": len(ladder_rows),
        }
    except Exception as exc:  # noqa: BLE001
        height = None
        _record_error(store, summary, "special:ladder", exc, fetched_at)

    try:
        as_of = datetime.now(TZ).date().isoformat()
        store.write_rows(
            "market_breadth_daily",
            [
                {
                    "as_of": as_of,
                    "total": None,
                    "up_count": None,
                    "down_count": None,
                    "flat_count": None,
                    "limit_up_count": counts.get("limit_up"),
                    "limit_down_count": counts.get("limit_down"),
                    "limit_break_count": counts.get("limit_break"),
                    "ladder_height": height,
                    "source": "hithink-special-data",
                    "ingest_time": fetched_at,
                }
            ],
            columns=[
                "as_of",
                "total",
                "up_count",
                "down_count",
                "flat_count",
                "limit_up_count",
                "limit_down_count",
                "limit_break_count",
                "ladder_height",
                "source",
                "ingest_time",
            ],
        )
    except Exception as exc:  # noqa: BLE001
        _record_error(store, summary, "special_merge", exc, fetched_at)


def _ladder_rows(height: int | None) -> list[dict[str, Any]]:
    return [
        {
            "height": height,
            "as_of": datetime.now(TZ).date().isoformat(),
            "source": "hithink-special-data",
        }
    ]


def _record_error(
    store: Store,
    summary: dict[str, Any],
    key: str,
    exc: Exception,
    fetched_at: str,
) -> None:
    message = f"{type(exc).__name__}: {exc}"
    summary["errors"][key] = message
    store.log(key, "error", rows=0, message=message, fetched_at=fetched_at)


def _record_warning(
    store: Store,
    summary: dict[str, Any],
    key: str,
    exc: Exception,
    fetched_at: str,
) -> None:
    """可选增强项失败：记录警告但不阻断整体流程。"""
    message = f"{type(exc).__name__}: {exc}"
    summary["warnings"][key] = message
    store.log(key, "warn", rows=0, message=message, fetched_at=fetched_at)


def _snapshot_depth(store: Store, kind: str, source: str) -> int:
    """统计某来源/类别的板块快照已累计多少个交易日。"""
    with store.connect() as con:
        depth = con.execute(
            "SELECT count(DISTINCT trade_date) AS depth FROM board_daily "
            "WHERE kind = ? AND source = ?",
            [kind, source],
        ).fetchone()
    return int(depth[0]) if depth else 0


def _board_name(catalog: list[dict[str, Any]], code: str) -> str:
    for row in catalog:
        if str(row.get("thscode")) == code:
            return str(row.get("name") or code)
    return code


def _num(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _index_row(row: dict[str, Any], thscode: str, fetched_at: str) -> dict[str, Any]:
    trade_date = date_text(row.get("date_ms"))
    return {
        "trade_date": trade_date,
        "thscode": thscode,
        "open": _num(row.get("open_price")),
        "high": _num(row.get("high_price")),
        "low": _num(row.get("low_price")),
        "close": _num(row.get("close_price")),
        "volume": _num(row.get("volume")),
        "amount": _num(row.get("turnover")),
        "source": "hithink-index",
        "as_of": trade_date,
        "ingest_time": fetched_at,
    }


def _stock_row(row: dict[str, Any], fetched_at: str) -> dict[str, Any]:
    return {
        "trade_date": str(row["date"])[:10],
        "symbol": str(row["symbol"]),
        "open": _num(row.get("open")),
        "high": _num(row.get("high")),
        "low": _num(row.get("low")),
        "close": _num(row.get("close")),
        "volume": _num(row.get("volume")),
        "turnover_rate_pct": _num(row.get("turnover_rate_pct")),
        "amount": _num(row.get("amount")),
        "source": row.get("source") or "tencent",
        "adjust": row.get("adjust") or "qfq",
        "as_of": str(row["date"])[:10],
        "ingest_time": fetched_at,
    }


def _profile_row(
    profile: dict[str, Any],
    symbol: str,
    fetched_at: str,
) -> dict[str, Any]:
    return {
        "as_of": datetime.now(TZ).date().isoformat(),
        "symbol": symbol,
        "name": profile.get("name"),
        "last_price": _num(profile.get("price")),
        "change_pct": None,
        "volume": _num(profile.get("volume")),
        "amount": _num(profile.get("amount")),
        "turnover_rate_pct": _num(profile.get("turnover_rate")),
        "total_market_cap": _num(profile.get("total_market_cap")),
        "float_market_cap": _num(profile.get("float_market_cap")),
        "total_shares": _num(profile.get("total_shares")),
        "float_shares": _num(profile.get("float_shares")),
        "delayed": bool(profile.get("delayed", False)),
        "source": "eastmoney",
        "ingest_time": fetched_at,
    }


def _board_snapshot_row(
    row: dict[str, Any],
    kind: str,
    as_of: str,
    fetched_at: str,
) -> dict[str, Any]:
    return {
        "trade_date": as_of,
        "thscode": str(row["thscode"]),
        "kind": kind,
        "close": _num(row.get("last_price")),
        "change_pct": (
            _num(row.get("price_change_ratio_pct")) / 100
            if _num(row.get("price_change_ratio_pct")) is not None
            else None
        ),
        "volume": _num(row.get("volume")),
        "amount": _num(row.get("turnover")),
        "turnover_rate_pct": None,
        "source": "hithink-index-snapshot",
        "as_of": as_of,
        "ingest_time": fetched_at,
    }


def _board_history_row(
    row: dict[str, Any],
    kind: str,
    code: str,
    fetched_at: str,
) -> dict[str, Any]:
    trade_date = date_text(row.get("date_ms"))
    return {
        "trade_date": trade_date,
        "thscode": code,
        "kind": kind,
        "close": _num(row.get("close_price")),
        "change_pct": None,
        "volume": _num(row.get("volume")),
        "amount": _num(row.get("turnover")),
        "turnover_rate_pct": None,
        "source": "hithink-index-history",
        "as_of": trade_date,
        "ingest_time": fetched_at,
    }


def _em_board_snapshot_row(
    row: dict[str, Any],
    kind: str,
    as_of: str,
    fetched_at: str,
) -> dict[str, Any]:
    change_pct = _num(row.get("change_pct"))
    return {
        "trade_date": as_of,
        "thscode": _em_board_code(row["code"]),
        "kind": kind,
        "close": _num(row.get("price")),
        "change_pct": change_pct / 100 if change_pct is not None else None,
        "volume": _num(row.get("volume")),
        "amount": _num(row.get("amount")),
        "turnover_rate_pct": _num(row.get("turnover_rate")),
        "source": "eastmoney-board-snapshot",
        "as_of": as_of,
        "ingest_time": fetched_at,
    }


def _em_board_history_row(
    row: dict[str, Any],
    kind: str,
    board_code: str,
    fetched_at: str,
) -> dict[str, Any]:
    trade_date = str(row["date"])[:10]
    change_pct = _num(row.get("change_pct"))
    return {
        "trade_date": trade_date,
        "thscode": board_code,
        "kind": kind,
        "close": _num(row.get("close")),
        "change_pct": change_pct / 100 if change_pct is not None else None,
        "volume": _num(row.get("volume")),
        "amount": _num(row.get("amount")),
        "turnover_rate_pct": _num(row.get("turnover_rate")),
        "source": "eastmoney-board-history",
        "as_of": trade_date,
        "ingest_time": fetched_at,
    }
