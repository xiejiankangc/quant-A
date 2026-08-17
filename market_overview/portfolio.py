"""组合输入格式：JSON 文件的读取、校验与权重解析。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def to_thscode(symbol: str) -> str:
    """把 6 位 A 股代码映射为同花顺 thscode。"""
    symbol = str(symbol).strip()
    if not (symbol.isdigit() and len(symbol) == 6):
        raise ValueError(f"股票代码需要是 6 位数字，收到: {symbol!r}")
    if symbol.startswith("92") or symbol[0] in "48":
        return f"{symbol}.BJ"
    if symbol[0] in "569":
        return f"{symbol}.SH"
    if symbol[0] in "0123":
        return f"{symbol}.SZ"
    raise ValueError(f"无法识别交易所的股票代码: {symbol!r}")


@dataclass(frozen=True)
class Holding:
    symbol: str
    name: str
    weight: float | None = None
    shares: float | None = None
    market_value: float | None = None

    @property
    def thscode(self) -> str:
        return to_thscode(self.symbol)


@dataclass(frozen=True)
class Portfolio:
    name: str
    benchmark: str
    window_days: int
    holdings: tuple[Holding, ...]

    @classmethod
    def from_file(cls, path: str | Path) -> "Portfolio":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("组合文件顶层需要是 JSON 对象")

        holdings_raw = raw.get("holdings")
        if not isinstance(holdings_raw, list) or not holdings_raw:
            raise ValueError("holdings 需要是非空数组")

        holdings: list[Holding] = []
        weight_total = 0.0
        weight_count = 0
        market_value_count = 0

        for item in holdings_raw:
            if not isinstance(item, dict):
                raise ValueError("holdings 每个元素需要是对象")
            symbol = str(item.get("symbol", "")).strip()
            to_thscode(symbol)
            name = str(item.get("name") or symbol).strip()
            weight = _optional_float(item.get("weight"), symbol, "weight")
            shares = _optional_float(item.get("shares"), symbol, "shares")
            market_value = _optional_float(
                item.get("market_value"), symbol, "market_value"
            )
            if weight is not None:
                if weight <= 0:
                    raise ValueError(f"{symbol} 的 weight 需要大于 0")
                weight_count += 1
                weight_total += weight
            if shares is not None and shares <= 0:
                raise ValueError(f"{symbol} 的 shares 需要大于 0")
            if market_value is not None:
                if market_value < 0:
                    raise ValueError(f"{symbol} 的 market_value 不能为负")
                market_value_count += 1
            holdings.append(
                Holding(
                    symbol=symbol,
                    name=name,
                    weight=weight,
                    shares=shares,
                    market_value=market_value,
                )
            )

        if weight_count and weight_count != len(holdings):
            raise ValueError("持仓要么全部使用 weight，要么全部使用股数/市值，不能混用")
        if market_value_count and market_value_count != len(holdings):
            raise ValueError("使用 market_value 时所有持仓都需要提供 market_value")

        if weight_count:
            if abs(weight_total - 1.0) > 1e-6:
                raise ValueError(
                    f"持仓权重合计需要等于 1，当前为 {weight_total:.6f}"
                )

        window_days = int(raw.get("window_days", 120))
        if window_days <= 0:
            raise ValueError("window_days 需要是正整数")

        benchmark = str(raw.get("benchmark", "000300.SH")).strip()
        if "." not in benchmark:
            raise ValueError(
                f"benchmark 需要是 thscode（如 000300.SH），收到: {benchmark!r}"
            )

        return cls(
            name=str(raw.get("name", "未命名组合")).strip(),
            benchmark=benchmark,
            window_days=window_days,
            holdings=tuple(holdings),
        )

    def resolve_weights(self, prices: dict[str, float]) -> dict[str, float]:
        """按最新价把三种输入模式统一成权重字典。"""
        if not self.holdings:
            return {}

        if all(h.weight is not None for h in self.holdings):
            return {h.symbol: float(h.weight) for h in self.holdings}

        values: list[tuple[str, float]] = []
        for holding in self.holdings:
            if holding.market_value is not None:
                value = holding.market_value
            elif holding.shares is not None:
                price = prices.get(holding.symbol)
                if price is None:
                    raise RuntimeError(f"缺少 {holding.symbol} 的最新价，无法折算权重")
                value = holding.shares * price
            else:
                raise ValueError(
                    f"{holding.symbol} 未提供 weight/shares/market_value 任一输入"
                )
            values.append((holding.symbol, value))

        total = sum(value for _, value in values)
        if total <= 0:
            raise ValueError("组合总市值需要大于 0")
        return {symbol: value / total for symbol, value in values}


def _optional_float(value: Any, symbol: str, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{symbol} 的 {field} 不是数字") from exc
