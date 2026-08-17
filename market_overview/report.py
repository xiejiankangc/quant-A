"""Markdown 报告渲染：从规范仓库读数据、计算指标并输出可读证据。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from . import indicators
from .portfolio import Portfolio
from .store import Store


TZ = ZoneInfo("Asia/Shanghai")


def render(
    store_path: str | Path,
    portfolio: Portfolio | None,
    out_path: str | Path,
) -> Path:
    store = Store(store_path)
    generated_at = datetime.now(TZ).isoformat(timespec="seconds")
    lines: list[str] = []
    lines.append(f"# 市场全景辅助决策报告：{portfolio.name if portfolio else '市场概览'}")
    lines.append("")
    lines.append(f"- 生成时间：{generated_at}")
    lines.append(f"- 统计窗口：{portfolio.window_days if portfolio else 120} 个交易日")
    lines.append(f"- 组合基准：{portfolio.benchmark if portfolio else '000300.SH'}")
    lines.append("")

    index_daily = store.query_df(
        "SELECT * FROM index_daily ORDER BY trade_date"
    )
    market_index = index_daily[
        index_daily["thscode"] == "000001.SH"
    ] if not index_daily.empty else pd.DataFrame()
    breadth = _latest_breadth(store)
    market = indicators.market_summary(
        market_index,
        breadth.iloc[0] if not breadth.empty else None,
    )
    lines.append("## 1. 市场概览")
    lines.append("")
    if market.get("available"):
        lines.append("| 指标 | 值 |")
        lines.append("| --- | --- |")
        lines.append(f"| 最新交易日 | {market.get('trade_date')} |")
        lines.append(f"| 上证指数 | {_fmt(market.get('close'))} |")
        lines.append(f"| 当日涨跌 | {_fmt_pct(market.get('change_pct'))} |")
        lines.append(f"| 20 日涨跌 | {_fmt_pct(market.get('return_20d'))} |")
        lines.append(f"| 量能趋势 | {market.get('amount_trend', '-')} |")
        lines.append(f"| 当日量/20日均量 | {_fmt_ratio(market.get('amount_ratio'))} |")
        lines.append(f"| 5/20 日均量比 | {_fmt_ratio(market.get('amount_ma5_20'))} |")
        lines.append(f"| 上涨/下跌/平盘 | {_int(market.get('up_count'))} / {_int(market.get('down_count'))} / {_int(market.get('flat_count'))} |")
        lines.append(f"| 涨停/跌停/炸板 | {_int(market.get('limit_up_count'))} / {_int(market.get('limit_down_count'))} / {_int(market.get('limit_break_count'))} |")
        lines.append(f"| 连板高度 | {_int(market.get('ladder_height'))} |")
        lines.append(f"| 炸板率 | {_fmt_pct(market.get('break_rate'))} |")
        lines.append(f"| 状态标签 | {market.get('regime', '-')} |")
        lines.append("")
        lines.append(
            f"> 状态标签由固定规则生成：量能{market.get('amount_trend', '-')}且上涨家数占比"
            f"{_fmt_pct(market.get('up_ratio'))} 时归为「{market.get('regime', '中性')}」。"
        )
    else:
        lines.append("> 暂无市场基准数据，请先运行 fetch。")
    lines.append("")

    lines.append("## 2. 板块概览")
    lines.append("")
    _render_boards(lines, store, market_index)

    lines.append("## 3. 板块内个股")
    lines.append("")
    _render_intra_board(lines, store)

    lines.append("## 4. 个股观察")
    lines.append("")
    _render_stocks(lines, store, portfolio)

    lines.append("## 5. 组合风险")
    lines.append("")
    _render_portfolio(lines, store, portfolio, index_daily)

    lines.append("## 6. 数据状态")
    lines.append("")
    _render_data_status(lines, store)

    lines.append("## 口径说明")
    lines.append("")
    lines.append(
        "- 收益基于后复权收盘价；停牌/缺失日按 0 收益计入并只在样本数中体现。"
    )
    lines.append(
        "- 组合 β 同时输出「加权个股 β」与「组合收益回归 β」两种口径。"
    )
    lines.append(
        "- 板块回撤为窗口内收盘相对前期最高点的最大跌幅；量能趋势基于成交额。"
    )
    lines.append(
        "- 未抓取或计算失败的项目显示「-」或「不可用」，不使用模拟值。"
    )
    lines.append("")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return out


def _latest_breadth(store: Store) -> pd.DataFrame:
    frame = store.query_df(
        "SELECT * FROM market_breadth_daily ORDER BY as_of DESC"
    )
    if frame.empty:
        return frame
    latest = frame.iloc[0]["as_of"]
    frame = frame[frame["as_of"] == latest]
    merged: dict[str, Any] = {}
    for column in (
        "total",
        "up_count",
        "down_count",
        "flat_count",
        "limit_up_count",
        "limit_down_count",
        "limit_break_count",
        "ladder_height",
    ):
        values = pd.to_numeric(frame[column], errors="coerce").dropna()
        if not values.empty:
            merged[column] = float(values.max())
    merged["as_of"] = latest
    return pd.DataFrame([merged])


def _render_boards(
    lines: list[str],
    store: Store,
    benchmark: pd.DataFrame,
) -> None:
    board_daily = store.query_df("SELECT * FROM board_daily ORDER BY trade_date")
    if board_daily.empty:
        lines.append("> 暂无板块数据，请先运行 fetch。")
        lines.append("")
        return
    board_meta = store.query_df("SELECT * FROM board_meta")
    names = (
        dict(zip(board_meta["thscode"], board_meta["name"]))
        if not board_meta.empty
        else {}
    )
    snapshot = board_daily[
        board_daily["source"] == "hithink-index-snapshot"
    ].copy()
    if snapshot.empty:
        snapshot = board_daily[
            board_daily["trade_date"] == board_daily["trade_date"].max()
        ].copy()
    snapshot["change_pct"] = pd.to_numeric(snapshot["change_pct"], errors="coerce")
    snapshot["amount"] = pd.to_numeric(snapshot["amount"], errors="coerce")
    snapshot = snapshot.drop_duplicates("thscode", keep="last")
    snapshot["name"] = snapshot["thscode"].map(names)

    lines.append("### 当日板块快照（涨幅/跌幅前列）")
    lines.append("")
    ordered = snapshot.sort_values("change_pct", ascending=False)
    lines.append("| 板块 | 代码 | 涨跌幅 | 成交额 |")
    lines.append("| --- | --- | --- | --- |")
    for _, row in ordered.head(10).iterrows():
        lines.append(
            f"| {row.get('name') or row.get('thscode')} | `{row.get('thscode')}` "
            f"| {_fmt_pct(row.get('change_pct'))} | {_fmt_amount(row.get('amount'))} |"
        )
    for _, row in ordered.tail(10).iterrows():
        lines.append(
            f"| {row.get('name') or row.get('thscode')} | `{row.get('thscode')}` "
            f"| {_fmt_pct(row.get('change_pct'))} | {_fmt_amount(row.get('amount'))} |"
        )
    lines.append("")

    history_codes = board_daily[board_daily["source"] == "hithink-index-history"][
        "thscode"
    ].unique()
    if len(history_codes) == 0:
        lines.append("> 本报告未抓取板块历史明细，因此暂无多日强度/回撤列。")
        lines.append("")
        return

    focused = board_daily[board_daily["thscode"].isin(history_codes)]
    metrics = indicators.board_metrics(focused, benchmark, names)
    lines.append("### 重点板块多日强度（5 日 / 20 日）")
    lines.append("")
    for n in (5, 20):
        subset = metrics[metrics["n"] == n].sort_values("relative", ascending=False)
        if subset.empty:
            continue
        lines.append(f"#### {n} 日窗口")
        lines.append("")
        lines.append("| 板块 | 涨幅 | 相对基准 | 最大回撤 | 量能趋势 | 回撤类型 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for _, row in subset.iterrows():
            lines.append(
                f"| {row.get('name')} | {_fmt_pct(row.get('return'))} "
                f"| {_fmt_pct(row.get('relative'))} | {_fmt_pct(row.get('max_drawdown'))} "
                f"| {row.get('amount_trend') or '-'} | {row.get('pullback') or '-'} |"
            )
        lines.append("")


def _render_intra_board(lines: list[str], store: Store) -> None:
    constituents = store.query_df(
        "SELECT * FROM board_constituent_daily ORDER BY as_of"
    )
    if constituents.empty:
        lines.append("> 暂无板块成分数据，请先运行 fetch。")
        lines.append("")
        return
    board_daily = store.query_df("SELECT * FROM board_daily ORDER BY trade_date")
    detail, group_summary = indicators.intra_board_metrics(
        constituents, board_daily
    )
    if detail.empty:
        lines.append("> 板块内指标暂无法计算。")
        lines.append("")
        return

    board_meta = store.query_df("SELECT * FROM board_meta")
    board_names = (
        dict(zip(board_meta["thscode"], board_meta["name"]))
        if not board_meta.empty
        else {}
    )
    for board_code, board_group in detail.groupby("board_thscode"):
        name = board_names.get(board_code, board_code)
        lines.append(f"### {name or board_code}（`{board_code}`）")
        lines.append("")
        ordered = board_group.sort_values("relative_alpha", ascending=False)
        lines.append("| 个股 | 涨跌幅 | 相对板块 | 成交额占比 | 市值分组 | 地位 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for _, row in ordered.iterrows():
            lines.append(
                f"| {row.get('name') or row.get('symbol')} | {_fmt_pct(row.get('change_pct'))} "
                f"| {_fmt_pct(row.get('relative_alpha'))} | {_fmt_pct(row.get('amount_share'))} "
                f"| {row.get('cap_group') or '-'} | {row.get('role') or '-'} |"
            )
        lines.append("")
        board_summary = group_summary[group_summary["board_thscode"] == board_code]
        if not board_summary.empty:
            lines.append("| 市值分组 | 家数 | 平均涨幅 | 平均相对板块 | 成交额占比 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for _, row in board_summary.iterrows():
                lines.append(
                    f"| {row.get('cap_group') or '-'} | {int(row.get('count') or 0)} "
                    f"| {_fmt_pct(row.get('mean_change'))} "
                    f"| {_fmt_pct(row.get('mean_relative_alpha'))} "
                    f"| {_fmt_pct(row.get('amount_share'))} |"
                )
            lines.append("")


def _render_stocks(
    lines: list[str],
    store: Store,
    portfolio: Portfolio | None,
) -> None:
    if portfolio is None:
        lines.append("> 未提供组合文件，暂无个股观察清单。")
        lines.append("")
        return
    symbols = [holding.symbol for holding in portfolio.holdings]
    stock_daily = store.query_df(
        "SELECT * FROM stock_daily WHERE symbol IN "
        + str(tuple(symbols)).replace(",)", ")")
        + " ORDER BY trade_date"
    )
    profiles = store.query_df("SELECT * FROM stock_snapshot ORDER BY as_of DESC")
    lines.append("| 代码 | 名称 | 最新价 | 换手率 | 5日换手 | 20日换手 | 20日涨幅 | 量比 | 形态 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for symbol in symbols:
        frame = stock_daily[stock_daily["symbol"] == symbol]
        profile = (
            profiles[profiles["symbol"] == symbol].iloc[0]
            if not profiles[profiles["symbol"] == symbol].empty
            else pd.Series(dtype=object)
        )
        metrics = indicators.stock_metrics(frame, profile)
        if not metrics.get("available"):
            lines.append(f"| {symbol} | - | - | - | - | - | - | - | 无数据 |")
            continue
        form = _form_text(metrics)
        lines.append(
            f"| {symbol} | {metrics.get('profile', {}).get('name') or symbol} "
            f"| {_fmt(metrics.get('close'))} | {_fmt_turnover(metrics.get('turnover_rate'))} "
            f"| {_fmt_turnover(metrics.get('turnover_5d'))} | {_fmt_turnover(metrics.get('turnover_20d'))} "
            f"| {_fmt_pct(metrics.get('return_20d'))} | {_fmt_ratio(metrics.get('volume_ratio'))} "
            f"| {form} |"
        )
    lines.append("")
    lines.append(
        "> 形态列只列客观派生值；筹码结构未获得可靠数据源时不输出近似结论。"
    )
    lines.append("")


def _render_portfolio(
    lines: list[str],
    store: Store,
    portfolio: Portfolio | None,
    index_daily: pd.DataFrame,
) -> None:
    if portfolio is None:
        lines.append("> 未提供组合文件，无法计算组合指标。")
        lines.append("")
        return
    stock_daily = store.query_df("SELECT * FROM stock_daily ORDER BY trade_date")
    profiles = store.query_df("SELECT * FROM stock_snapshot ORDER BY as_of DESC")
    prices: dict[str, float] = {}
    weights: dict[str, float] = {}
    for holding in portfolio.holdings:
        if holding.weight is not None:
            weights[holding.symbol] = holding.weight
        elif not profiles[profiles["symbol"] == holding.symbol].empty:
            value = profiles[profiles["symbol"] == holding.symbol].iloc[0]["last_price"]
            if value is not None and not pd.isna(value):
                prices[holding.symbol] = float(value)
    if not weights:
        weights = portfolio.resolve_weights(prices)
    lines.append("### 持仓权重")
    lines.append("")
    lines.append("| 代码 | 名称 | 权重 |")
    lines.append("| --- | --- | --- |")
    for holding in portfolio.holdings:
        lines.append(
            f"| {holding.symbol} | {holding.name} | {_fmt_pct(weights.get(holding.symbol))} |"
        )
    lines.append("")
    lines.append("> 权重合计为 1；股数/市值输入已按最新价折算。")
    lines.append("")

    benchmark = index_daily[
        index_daily["thscode"] == portfolio.benchmark
    ] if not index_daily.empty else pd.DataFrame()
    frames: dict[str, pd.DataFrame] = {}
    for symbol in weights:
        frames[symbol] = stock_daily[stock_daily["symbol"] == symbol]
    metrics = indicators.portfolio_metrics(
        frames,
        benchmark,
        weights,
        window_days=portfolio.window_days,
    )
    if not metrics.get("available"):
        lines.append("> 组合历史数据不足，β 与集中度无法可靠计算。")
        lines.append("")
        return

    lines.append("### β")
    lines.append("")
    lines.append("| 口径 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 加权个股 β | {_fmt_ratio(metrics.get('weighted_beta'))} |")
    lines.append(f"| 组合收益回归 β | {_fmt_ratio(metrics.get('regression_beta'))} |")
    lines.append(f"| 日 α | {_fmt_pct(metrics.get('alpha_daily'))} |")
    lines.append(f"| R² | {_fmt_ratio(metrics.get('r_squared'))} |")
    lines.append(f"| 与基准相关系数 | {_fmt_ratio(metrics.get('corr_with_benchmark'))} |")
    lines.append(f"| 样本天数 | {metrics.get('sample_days')} |")
    lines.append("")

    lines.append("### 集中度与分散")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| HHI | {_fmt_ratio(metrics.get('hhi'))} |")
    lines.append(f"| 有效持仓数 | {_fmt_ratio(metrics.get('effective_n'))} |")
    lines.append(f"| 最大权重 | {_fmt_pct(metrics.get('max_weight'))} |")
    lines.append(f"| 平均两两相关系数 | {_fmt_ratio(metrics.get('avg_pairwise_corr'))} |")
    lines.append(f"| 分散化比率 | {_fmt_ratio(metrics.get('diversification_ratio'))} |")
    lines.append(f"| 组合年化波动率 | {_fmt_pct(metrics.get('portfolio_vol_annual'))} |")
    lines.append(f"| 基准年化波动率 | {_fmt_pct(metrics.get('benchmark_vol_annual'))} |")
    lines.append("")

    corr = metrics.get("correlation_matrix")
    if isinstance(corr, pd.DataFrame) and not corr.empty:
        lines.append("### 相关系数矩阵")
        lines.append("")
        lines.append("| | " + " | ".join(str(c) for c in corr.columns) + " |")
        lines.append("|" + " --- |" * (len(corr.columns) + 1))
        for symbol, row in corr.iterrows():
            values = " | ".join(_fmt_ratio(value) for value in row)
            lines.append(f"| {symbol} | {values} |")
        lines.append("")

    constituents = store.query_df(
        "SELECT * FROM board_constituent_daily ORDER BY as_of"
    )
    exposure = indicators.sector_exposure(constituents, metrics["weights"])
    if exposure:
        board_meta = store.query_df("SELECT * FROM board_meta")
        names = (
            dict(zip(board_meta["thscode"], board_meta["name"]))
            if not board_meta.empty
            else {}
        )
        lines.append("### 板块暴露")
        lines.append("")
        lines.append("| 板块 | 权重 |")
        lines.append("| --- | --- |")
        for board, weight in sorted(exposure.items(), key=lambda item: item[1], reverse=True):
            lines.append(f"| {names.get(board, board)} | {_fmt_pct(weight)} |")
        lines.append("")
    lines.append(
        "> 板块暴露只统计本次已抓取成分的重点板块；未覆盖的持仓归属不会伪造为 0，"
        "需要时可在 fetch 时用 `--watch-boards` 扩大覆盖。"
    )
    lines.append("")


def _render_data_status(lines: list[str], store: Store) -> None:
    log = store.query_df(
        "SELECT key, status, rows, message, fetched_at FROM fetch_log"
    )
    if log.empty:
        lines.append("> 暂无抓取记录。")
        lines.append("")
        return
    log["fetched_at"] = pd.to_datetime(log["fetched_at"], errors="coerce")
    latest = log.sort_values("fetched_at").drop_duplicates("key", keep="last")
    errors = latest[latest["status"] == "error"]
    ok_count = int((latest["status"] == "ok").sum())
    lines.append(f"- 成功数据项：{ok_count}；失败数据项：{len(errors)}")
    if not errors.empty:
        lines.append("")
        lines.append("| 数据项 | 原因 |")
        lines.append("| --- | --- |")
        for _, row in errors.iterrows():
            lines.append(
                f"| `{row.get('key')}` | {(row.get('message') or '未知错误')[:240]} |"
            )
    lines.append("")


def _form_text(metrics: dict[str, Any]) -> str:
    parts: list[str] = []
    for n in (5, 10, 20, 60):
        if metrics.get(f"above_ma{n}"):
            parts.append(f">MA{n}")
        elif f"above_ma{n}" in metrics:
            parts.append(f"<MA{n}")
    high = metrics.get("high_20d")
    if high is not None and metrics.get("close") is not None and metrics["close"] >= high:
        parts.append("20日新高")
    drawdown = metrics.get("max_drawdown_60d")
    if drawdown is not None:
        parts.append(f"60日回撤{_fmt_pct(drawdown)}")
    return "、".join(parts) if parts else "-"


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    try:
        number = float(value)
        return f"{number:,.2f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_amount(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "-"
    if abs(number) >= 1e8:
        return f"{number / 1e8:,.2f} 亿"
    if abs(number) >= 1e4:
        return f"{number / 1e4:,.2f} 万"
    return f"{number:,.2f}"


def _fmt_pct(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "-"
    return f"{number * 100:,.2f}%"


def _fmt_turnover(value: Any) -> str:
    """换手率字段按百分数存储，直接格式化，不再乘 100。"""
    number = _num(value)
    return f"{number:,.2f}%" if number is not None else "-"


def _fmt_ratio(value: Any) -> str:
    number = _num(value)
    return f"{number:,.3f}" if number is not None else "-"


def _int(value: Any) -> str:
    number = _num(value)
    return f"{int(number)}" if number is not None else "-"


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
        return number if pd.notna(number) else None
    except (TypeError, ValueError):
        return None
