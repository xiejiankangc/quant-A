"""Markdown 报告骨架（M0）。

指标计算在后续里程碑补齐，这里只渲染已抓取的数据与明确的占位说明，不伪造
任何统计结果。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .cache import Cache
from .portfolio import Portfolio


def _fmt(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f}"


def _fmt_date(date_ms: int | None) -> str:
    if not date_ms:
        return "-"
    try:
        return datetime.fromtimestamp(
            int(date_ms) / 1000,
            ZoneInfo("Asia/Shanghai"),
        ).strftime("%Y-%m-%d")
    except (ValueError, OSError):
        return str(date_ms)


def render(cache_root: str | Path, portfolio: Portfolio, out_path: str | Path) -> Path:
    cache = Cache(cache_root)
    generated_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
        timespec="seconds"
    )
    lines: list[str] = []
    lines.append(f"# 市场概览报告：{portfolio.name}")
    lines.append("")
    lines.append(f"- 生成时间：{generated_at}")
    lines.append(f"- 统计窗口：{portfolio.window_days} 个交易日（日历回看约 2 倍）")
    lines.append(f"- 基准：{portfolio.benchmark}")
    lines.append("")

    lines.append("## 市场基准（M1：量能指标待落地）")
    lines.append("")
    if cache.exists("benchmark/index_history.jsonl"):
        benchmark = cache.read_rows("benchmark/index_history.jsonl")
        lines.append(f"已抓到 {len(benchmark)} 行基准日线。最新一条：")
        lines.append("")
        last = benchmark[-1]
        lines.append(
            f"- 日期 `{_fmt_date(last.get('date_ms'))}`，收盘 {_fmt(last.get('close_price'))}，"
            f"成交额 {_fmt(last.get('turnover'))}"
        )
    else:
        lines.append("> 未找到基准缓存，请先运行 fetch。")
    lines.append("")

    lines.append("## 板块概览（M2：强度/回撤/量能趋势待落地）")
    lines.append("")
    for kind, label in (("industry", "行业板块"), ("concept", "概念板块")):
        rel = f"boards/{kind}.jsonl"
        if not cache.exists(rel):
            lines.append(f"### {label}")
            lines.append("")
            lines.append("> 未找到板块列表缓存，请先运行 fetch。")
            lines.append("")
            continue
        rows = cache.read_rows(rel)
        lines.append(f"### {label}（{len(rows)} 个）")
        lines.append("")
        lines.append("| 代码 | 名称 | 最新 | 涨跌幅 | 成交额 |")
        lines.append("| --- | --- | --- | --- | --- |")
        top = sorted(
            rows,
            key=lambda row: abs(row.get("change_pct") or 0.0),
            reverse=True,
        )[:5]
        for row in top:
            lines.append(
                f"| {row.get('code')} | {row.get('name')} | {_fmt(row.get('price'))} "
                f"| {_fmt(row.get('change_pct'))}% | {_fmt(row.get('amount'))} |"
            )
        lines.append("")

    lines.append("## 板块明细样例（M2 占位）")
    lines.append("")
    summary = cache.read_json("fetch_summary.json") or {}
    artifacts = summary.get("artifacts") or {}
    board_items = [
        key for key in artifacts if key.startswith("boards_")
    ]
    if not board_items:
        lines.append("> 暂无板块明细样例。")
    else:
        lines.append("| 板块代码 | 历史行数 | 成分行数 |")
        lines.append("| --- | --- | --- |")
        by_code: dict[str, dict[str, int]] = {}
        for key in board_items:
            parts = key.split("_")
            if len(parts) < 4:  # 跳过 boards_industry / boards_concept 列表项
                continue
            code = parts[2] if len(parts) > 2 else key
            row_type = "history" if key.endswith("_history") else "constituents"
            by_code.setdefault(code, {})[row_type] = artifacts[key].get("rows", 0)
        for code, counts in sorted(by_code.items()):
            lines.append(
                f"| `{code}` | {counts.get('history', 0)} "
                f"| {counts.get('constituents', 0)} |"
            )
    lines.append("")

    lines.append("## 组合持仓（M5：β 与集中度待落地）")
    lines.append("")
    lines.append("| 代码 | 名称 | 权重 | 最新价 | 总市值 | 换手率 | 日线行数 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for holding in portfolio.holdings:
        profile = _first_row(cache, f"stocks/{holding.symbol}/profile.jsonl")
        history_rows = (
            len(cache.read_rows(f"stocks/{holding.symbol}/history.jsonl"))
            if cache.exists(f"stocks/{holding.symbol}/history.jsonl")
            else 0
        )
        lines.append(
            f"| {holding.symbol} | {holding.name} | {holding.weight:.2%} "
            f"| {_fmt((profile or {}).get('price'))} "
            f"| {_fmt((profile or {}).get('total_market_cap'))} "
            f"| {_fmt((profile or {}).get('turnover_rate'))}% "
            f"| {history_rows} |"
        )
    lines.append("")
    lines.append(
        "> β、相关系数矩阵与集中度指标按设计稿在 M5 实现；本版不输出任何"
        "未经计算的估计值。"
    )
    lines.append("")

    lines.append("## 抓取状态")
    lines.append("")
    errors = summary.get("errors") or {}
    if not errors:
        lines.append("本轮抓取未记录到失败项。")
    else:
        lines.append("以下数据源本次抓取失败，原因如实列出：")
        lines.append("")
        lines.append("| 数据项 | 原因 |")
        lines.append("| --- | --- |")
        for key, reason in errors.items():
            lines.append(f"| `{key}` | {reason} |")
    lines.append("")

    lines.append("## 数据文件")
    lines.append("")
    lines.append(f"- 缓存根目录：`{Path(cache_root)}`")
    lines.append(
        "- 结构：`benchmark/`、`boards/{industry,concept}/`、`stocks/`，"
        "每份 JSONL 附 `.meta.json` 记录抓取参数与时间"
    )
    lines.append("")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return out


def _first_row(cache: Cache, rel: str) -> dict | None:
    if not cache.exists(rel):
        return None
    rows = cache.read_rows(rel)
    return rows[0] if rows else None
