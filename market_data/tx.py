"""腾讯日线历史行情访问层。

用途：当同花顺本地 DuckDB 未初始化、或东财 push2his 被 VPN 风控时，作为
A 股日线历史的稳健回退源。只提供读取，不承载业务指标。

数据来自 akshare 的腾讯通道；字段统一转换为本项目的 snake_case 口径：
date/open/close/high/low/volume/turnover_rate_pct/amount。
"""

from __future__ import annotations

from typing import Any

import akshare as ak


def _tx_symbol(symbol: str) -> str:
    code = str(symbol).strip()
    if not (code.isdigit() and len(code) == 6):
        raise ValueError(f"股票代码需要是 6 位数字，收到: {symbol!r}")
    if code[0] in "569":
        return f"sh{code}"
    if code[0] in "48":
        return f"bj{code}"
    return f"sz{code}"


def daily_history(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    adjust: str = "qfq",
) -> list[dict[str, Any]]:
    """取单只 A 股日线历史。

    adjust 取值：qfq / hfq / ""；默认 qfq（前复权），用于收益计算。
    """
    if adjust not in ("qfq", "hfq", ""):
        raise ValueError(f"adjust 只支持 qfq/hfq/空串，收到: {adjust!r}")
    tx_symbol = _tx_symbol(symbol)
    df = ak.stock_zh_a_hist_tx(
        symbol=tx_symbol,
        start_date=start_date,
        end_date=end_date,
        adjust=adjust,
    )
    if df is None or df.empty:
        return []

    rows: list[dict[str, Any]] = []
    for record in df.to_dict("records"):
        raw_date = record.get("date")
        if hasattr(raw_date, "strftime"):
            date_str = raw_date.strftime("%Y-%m-%d")
        else:
            date_str = str(raw_date)[:10]
        turnover = record.get("turnover")
        rows.append(
            {
                "symbol": str(symbol).strip(),
                "date": date_str,
                "open": _num(record.get("open")),
                "close": _num(record.get("close")),
                "high": _num(record.get("high")),
                "low": _num(record.get("low")),
                "volume": _num(record.get("volume")),
                "turnover_rate_pct": (
                    round(float(turnover) * 100, 4) if turnover is not None else None
                ),
                "amount": _num(record.get("amount")),
                "source": "tencent",
                "adjust": adjust,
            }
        )
    return rows


def _num(value: Any) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
