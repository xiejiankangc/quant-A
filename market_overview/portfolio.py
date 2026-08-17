"""组合输入格式：JSON 文件的读取与校验。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


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
    weight: float
    name: str

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
        total_weight = 0.0
        for item in holdings_raw:
            if not isinstance(item, dict):
                raise ValueError("holdings 每个元素需要是对象")
            symbol = str(item.get("symbol", "")).strip()
            to_thscode(symbol)  # 提前校验代码格式
            try:
                weight = float(item.get("weight"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{symbol} 的 weight 不是数字") from exc
            if weight <= 0:
                raise ValueError(f"{symbol} 的 weight 需要大于 0")
            name = str(item.get("name") or symbol).strip()
            holdings.append(Holding(symbol=symbol, weight=weight, name=name))
            total_weight += weight

        if abs(total_weight - 1.0) > 1e-6:
            raise ValueError(f"持仓权重合计需要等于 1，当前为 {total_weight:.6f}")

        window_days = int(raw.get("window_days", 60))
        if window_days <= 0:
            raise ValueError("window_days 需要是正整数")

        benchmark = str(raw.get("benchmark", "000001.SH")).strip()
        if "." not in benchmark:
            raise ValueError(f"benchmark 需要是 thscode（如 000001.SH），收到: {benchmark!r}")

        return cls(
            name=str(raw.get("name", "未命名组合")).strip(),
            benchmark=benchmark,
            window_days=window_days,
            holdings=tuple(holdings),
        )
