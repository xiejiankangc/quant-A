"""指标计算：市场、板块、板块内个股、个股与组合。"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def _num(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _fmt_pct(value: float | None, digits: int = 2) -> float | None:
    number = _num(value)
    return round(number * 100, digits) if number is not None else None


def volume_metrics(amounts: pd.Series) -> dict[str, float | None]:
    """成交额序列的量能趋势指标。"""
    amounts = pd.to_numeric(amounts, errors="coerce").dropna()
    if amounts.empty:
        return {}
    last = float(amounts.iloc[-1])
    ma5 = float(amounts.tail(5).mean())
    ma20 = float(amounts.tail(20).mean())
    prev = float(amounts.iloc[-2]) if len(amounts) >= 2 else None
    five_ago = float(amounts.iloc[-6]) if len(amounts) >= 6 else None
    trailing60 = amounts.tail(60)
    trailing120 = amounts.tail(120)
    ma5_20 = ma5 / ma20 if ma20 else None
    trend = "平稳"
    if ma5_20 is not None:
        if ma5_20 > 1.05:
            trend = "扩张"
        elif ma5_20 < 0.95:
            trend = "收缩"
    return {
        "amount": last,
        "amount_ma5": ma5,
        "amount_ma20": ma20,
        "amount_ratio": last / ma20 if ma20 else None,
        "amount_ma5_20": ma5_20,
        "amount_dod": last / prev - 1 if prev else None,
        "amount_5d": last / five_ago - 1 if five_ago else None,
        "amount_pct60": float((trailing60 < last).mean()) if len(trailing60) else None,
        "amount_pct120": float((trailing120 < last).mean()) if len(trailing120) else None,
        "amount_trend": trend,
    }


def market_summary(
    benchmark_df: pd.DataFrame,
    breadth_row: pd.Series | None = None,
) -> dict[str, Any]:
    """市场层摘要：指数趋势、量能、宽度、情绪与状态标签。"""
    df = benchmark_df.sort_values("trade_date")
    if df.empty:
        return {"available": False}

    closes = pd.to_numeric(df["close"], errors="coerce").dropna()
    last_close = float(closes.iloc[-1])
    prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else None
    change_pct = last_close / prev_close - 1 if prev_close else None
    ret20 = last_close / float(closes.iloc[-21]) - 1 if len(closes) >= 21 else None

    volume = volume_metrics(df["amount"])
    breadth = {
        "total": _num(breadth_row.get("total")),
        "up_count": _num(breadth_row.get("up_count")),
        "down_count": _num(breadth_row.get("down_count")),
        "flat_count": _num(breadth_row.get("flat_count")),
        "limit_up_count": _num(breadth_row.get("limit_up_count")),
        "limit_down_count": _num(breadth_row.get("limit_down_count")),
        "limit_break_count": _num(breadth_row.get("limit_break_count")),
        "ladder_height": _num(breadth_row.get("ladder_height")),
    } if breadth_row is not None else {}

    up = breadth.get("up_count")
    total = breadth.get("total")
    up_ratio = up / total if up is not None and total else None
    limit_up = breadth.get("limit_up_count")
    limit_break = breadth.get("limit_break_count")
    break_rate = (
        limit_break / (limit_up + limit_break)
        if limit_up is not None and limit_break is not None and (limit_up + limit_break) > 0
        else None
    )

    regime = "中性"
    trend = volume.get("amount_trend")
    if trend == "扩张" and up_ratio is not None and up_ratio >= 0.55:
        regime = "扩张"
    elif trend == "收缩" and up_ratio is not None and up_ratio <= 0.45:
        regime = "收缩"

    return {
        "available": True,
        "trade_date": str(df.iloc[-1]["trade_date"]),
        "close": last_close,
        "change_pct": change_pct,
        "return_20d": ret20,
        **volume,
        **breadth,
        "up_ratio": up_ratio,
        "break_rate": break_rate,
        "regime": regime,
    }


def board_metrics(
    board_daily: pd.DataFrame,
    benchmark_df: pd.DataFrame,
    names: dict[str, str] | None = None,
    n_list: tuple[int, ...] = (5, 10, 20),
) -> pd.DataFrame:
    """板块强度、相对强度、回撤与量能趋势。"""
    names = names or {}
    benchmark = benchmark_df.sort_values("trade_date")[["trade_date", "close"]].rename(
        columns={"close": "bench_close"}
    )
    benchmark["bench_close"] = pd.to_numeric(
        benchmark["bench_close"], errors="coerce"
    )
    rows: list[dict[str, Any]] = []

    for thscode, group in board_daily.sort_values("trade_date").groupby("thscode"):
        group = group.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
        group["close"] = pd.to_numeric(group["close"], errors="coerce")
        group["amount"] = pd.to_numeric(group["amount"], errors="coerce")
        merged = group.merge(benchmark, on="trade_date", how="left")
        closes = merged["close"].dropna()
        if closes.empty:
            continue
        last_close = float(closes.iloc[-1])
        for n in n_list:
            if len(closes) <= n:
                continue
            ret_n = last_close / float(closes.iloc[-1 - n]) - 1
            bench_n = merged["bench_close"].dropna()
            bench_ret = (
                float(bench_n.iloc[-1]) / float(bench_n.iloc[-1 - n]) - 1
                if len(bench_n) > n
                else None
            )
            window = closes.tail(n + 1)
            drawdown = float(window.min() / window.cummax().max() - 1) if len(window) else None
            volume = volume_metrics(merged["amount"].dropna())
            pullback = "非回撤"
            if ret_n < 0 and volume.get("amount_trend") in ("收缩", "平稳"):
                pullback = "缩量回撤"
            elif ret_n < 0 and volume.get("amount_trend") == "扩张":
                pullback = "放量回撤"
            rows.append(
                {
                    "thscode": thscode,
                    "name": names.get(thscode, thscode),
                    "n": n,
                    "return": ret_n,
                    "relative": ret_n - bench_ret if bench_ret is not None else None,
                    "max_drawdown": drawdown,
                    "gain_drawdown_ratio": (
                        ret_n / max(abs(drawdown), 1e-9) if drawdown is not None else None
                    ),
                    "amount_ratio": volume.get("amount_ratio"),
                    "amount_ma5_20": volume.get("amount_ma5_20"),
                    "amount_trend": volume.get("amount_trend"),
                    "pullback": pullback,
                }
            )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def intra_board_metrics(
    constituents: pd.DataFrame,
    board_daily: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """板块内个股当前关系与市值分组汇总。"""
    if constituents.empty:
        return pd.DataFrame(), pd.DataFrame()
    latest_board = (
        board_daily.dropna(subset=["change_pct"])
        .sort_values("trade_date")
        .drop_duplicates("thscode", keep="last")
        .set_index("thscode")
    )
    data = constituents.copy()
    data["change_pct"] = pd.to_numeric(data["change_pct"], errors="coerce")
    data["amount"] = pd.to_numeric(data["amount"], errors="coerce")
    data["total_market_cap"] = pd.to_numeric(
        data["total_market_cap"], errors="coerce"
    )

    caps = data["total_market_cap"].dropna()
    q30 = float(caps.quantile(0.30)) if len(caps) else None
    q70 = float(caps.quantile(0.70)) if len(caps) else None

    def cap_group(value: float | None) -> str | None:
        if value is None or q30 is None or q70 is None:
            return None
        if value >= q70:
            return "大盘"
        if value >= q30:
            return "中盘"
        return "小盘"

    data["cap_group"] = data["total_market_cap"].apply(cap_group)
    rows = []
    for thscode, group in data.groupby("board_thscode"):
        board_change = _num(
            latest_board.loc[thscode, "change_pct"] if thscode in latest_board.index else None
        )
        board_amount = _num(
            latest_board.loc[thscode, "amount"] if thscode in latest_board.index else None
        )
        for _, row in group.iterrows():
            change = _num(row.get("change_pct"))
            alpha = change - board_change if change is not None and board_change is not None else None
            amount_share = (
                _num(row.get("amount")) / board_amount
                if _num(row.get("amount")) is not None and board_amount
                else None
            )
            role = "中性"
            if board_change is not None and board_change > 0 and change is not None:
                if alpha is not None and alpha > 0:
                    role = "正超额/领涨"
                elif change > 0:
                    role = "跟随"
                else:
                    role = "拖累"
            elif board_change is not None and board_change < 0 and change is not None:
                if change > 0:
                    role = "逆势抗跌"
                elif alpha is not None and alpha > 0:
                    role = "相对抗跌"
                else:
                    role = "领跌"
            rows.append(
                {
                    "board_thscode": thscode,
                    "symbol": row.get("symbol"),
                    "name": row.get("name"),
                    "change_pct": change,
                    "relative_alpha": alpha,
                    "amount": _num(row.get("amount")),
                    "amount_share": amount_share,
                    "total_market_cap": _num(row.get("total_market_cap")),
                    "cap_group": row.get("cap_group"),
                    "role": role,
                }
            )
    result = pd.DataFrame(rows)
    if result.empty:
        return result, pd.DataFrame()

    group_summary = (
        result.groupby(["board_thscode", "cap_group"], dropna=False)
        .agg(
            count=("symbol", "size"),
            mean_change=("change_pct", "mean"),
            mean_relative_alpha=("relative_alpha", "mean"),
            amount_share=("amount_share", "sum"),
        )
        .reset_index()
    )
    return result, group_summary


def stock_metrics(
    stock_daily: pd.DataFrame,
    profile: pd.Series | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """个股客观形态与换手率趋势指标。"""
    df = stock_daily.sort_values("trade_date").drop_duplicates(
        "trade_date", keep="last"
    )
    if df.empty:
        return {"available": False}
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["high"] = pd.to_numeric(df["high"], errors="coerce")
    df["low"] = pd.to_numeric(df["low"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["turnover_rate_pct"] = pd.to_numeric(
        df["turnover_rate_pct"], errors="coerce"
    )
    closes = df["close"].dropna()
    if closes.empty:
        return {"available": False}
    last = float(closes.iloc[-1])

    alignment = {}
    for n in (5, 10, 20, 60):
        if len(closes) >= n:
            alignment[f"above_ma{n}"] = last > float(closes.tail(n).mean())
    high20 = df["high"].tail(20).max()
    low20 = df["low"].tail(20).min()
    high60 = df["high"].tail(60).max()
    low60 = df["low"].tail(60).min()
    last_row = df.iloc[-1]
    amplitude = (
        float(last_row["high"] - last_row["low"]) / last if last_row["high"] is not None and last_row["low"] is not None else None
    )
    window60 = closes.tail(60)
    drawdown = float(window60.min() / window60.max() - 1) if len(window60) else None
    volume20 = df["volume"].tail(20).dropna()
    volume_ratio = (
        float(df["volume"].iloc[-1]) / float(volume20.mean())
        if len(volume20) and volume20.mean()
        else None
    )
    turnover = df["turnover_rate_pct"].dropna()
    turnover_now = float(turnover.iloc[-1]) if len(turnover) else None
    turnover_5 = float(turnover.tail(5).mean()) if len(turnover) >= 5 else None
    turnover_20 = float(turnover.tail(20).mean()) if len(turnover) >= 20 else None

    profile_dict = profile if isinstance(profile, dict) else (
        profile.to_dict() if isinstance(profile, pd.Series) else {}
    )
    return {
        "available": True,
        "trade_date": str(df.iloc[-1]["trade_date"]),
        "close": last,
        "return_1d": float(closes.iloc[-1] / closes.iloc[-2] - 1) if len(closes) >= 2 else None,
        "return_20d": float(closes.iloc[-1] / closes.iloc[-21] - 1) if len(closes) >= 21 else None,
        **alignment,
        "high_20d": float(high20) if high20 is not None and not pd.isna(high20) else None,
        "low_20d": float(low20) if low20 is not None and not pd.isna(low20) else None,
        "high_60d": float(high60) if high60 is not None and not pd.isna(high60) else None,
        "low_60d": float(low60) if low60 is not None and not pd.isna(low60) else None,
        "amplitude": amplitude,
        "max_drawdown_60d": drawdown,
        "volume_ratio": volume_ratio,
        "turnover_rate": turnover_now,
        "turnover_5d": turnover_5,
        "turnover_20d": turnover_20,
        "profile": profile_dict,
    }


def portfolio_metrics(
    stock_frames: dict[str, pd.DataFrame],
    benchmark_df: pd.DataFrame,
    weights: dict[str, float],
    window_days: int = 120,
) -> dict[str, Any]:
    """组合 β、相关性、集中度与分散化比率。"""
    returns = {}
    for symbol, frame in stock_frames.items():
        if frame.empty:
            continue
        series = (
            frame.sort_values("trade_date")
            .drop_duplicates("trade_date", keep="last")
            .set_index("trade_date")["close"]
        )
        series = pd.to_numeric(series, errors="coerce").pct_change()
        if not series.dropna().empty:
            returns[symbol] = series
    if not returns:
        return {"available": False}

    ret_df = pd.DataFrame(returns).sort_index().fillna(0.0)
    ret_df = ret_df.tail(window_days)

    benchmark = (
        benchmark_df.sort_values("trade_date")
        .drop_duplicates("trade_date", keep="last")
        .set_index("trade_date")["close"]
    )
    mkt = pd.to_numeric(benchmark, errors="coerce").pct_change().rename("benchmark")
    common = ret_df.index.intersection(mkt.index)
    ret_df = ret_df.loc[common]
    mkt = mkt.loc[common]
    if ret_df.empty or len(common) < 30:
        return {"available": False, "sample_days": len(common)}

    available_weights = {s: weights[s] for s in ret_df.columns if s in weights}
    weight_sum = sum(available_weights.values())
    if weight_sum <= 0:
        return {"available": False, "reason": "no matching weights"}
    used_weights = {s: w / weight_sum for s, w in available_weights.items()}

    port_returns = sum(
        ret_df[symbol] * weight for symbol, weight in used_weights.items()
    )
    var_m = float(np.var(mkt, ddof=1))
    individual_betas: dict[str, float] = {}
    for symbol in ret_df.columns:
        cov = float(np.cov(ret_df[symbol], mkt, ddof=1)[0, 1])
        individual_betas[symbol] = cov / var_m if var_m else None
    weighted_beta = sum(
        used_weights.get(symbol, 0.0) * (individual_betas.get(symbol) or 0.0)
        for symbol in used_weights
    )
    cov_pm = float(np.cov(port_returns, mkt, ddof=1)[0, 1])
    regression_beta = cov_pm / var_m if var_m else None
    mean_p = float(port_returns.mean())
    mean_m = float(mkt.mean())
    alpha = mean_p - regression_beta * mean_m if regression_beta is not None else None
    corr_pm = float(np.corrcoef(port_returns, mkt)[0, 1]) if len(port_returns) > 1 else None

    corr_df = ret_df.corr()
    if corr_df.shape[0] >= 2:
        upper = corr_df.where(np.triu(np.ones(corr_df.shape), k=1).astype(bool))
        values = upper.stack().dropna()
        avg_pair_corr = float(values.mean()) if not values.empty else None
        top_pairs = values.abs().sort_values(ascending=False).head(3)
        pairs = [
            {"a": str(a), "b": str(b), "corr": float(value)}
            for (a, b), value in top_pairs.items()
        ]
    else:
        avg_pair_corr = None
        pairs = []

    hhi = sum(w * w for w in used_weights.values())
    effective_n = 1 / hhi if hhi else None
    port_vol = float(port_returns.std(ddof=1) * math.sqrt(252))
    mkt_vol = float(mkt.std(ddof=1) * math.sqrt(252))
    weighted_vol = sum(
        used_weights.get(symbol, 0.0)
        * float(ret_df[symbol].std(ddof=1) * math.sqrt(252))
        for symbol in ret_df.columns
    )
    diversification_ratio = port_vol / weighted_vol if weighted_vol else None

    return {
        "available": True,
        "sample_days": len(common),
        "window_days": window_days,
        "weights": used_weights,
        "individual_betas": individual_betas,
        "weighted_beta": weighted_beta,
        "regression_beta": regression_beta,
        "alpha_daily": alpha,
        "r_squared": corr_pm * corr_pm if corr_pm is not None else None,
        "corr_with_benchmark": corr_pm,
        "avg_pairwise_corr": avg_pair_corr,
        "top_pairs": pairs,
        "correlation_matrix": corr_df,
        "hhi": hhi,
        "effective_n": effective_n,
        "max_weight": max(used_weights.values()) if used_weights else None,
        "portfolio_vol_annual": port_vol,
        "benchmark_vol_annual": mkt_vol,
        "diversification_ratio": diversification_ratio,
    }


def sector_exposure(
    constituent_daily: pd.DataFrame,
    weights: dict[str, float],
) -> dict[str, float]:
    """按最新成分关系计算组合在板块上的权重暴露。"""
    if constituent_daily.empty:
        return {}
    latest = (
        constituent_daily.sort_values("as_of")
        .drop_duplicates(["board_thscode", "symbol"], keep="last")
    )
    exposure: dict[str, float] = {}
    for symbol, weight in weights.items():
        rows = latest[latest["symbol"] == symbol]
        if rows.empty:
            continue
        for _, row in rows.iterrows():
            board = str(row["board_thscode"])
            exposure[board] = exposure.get(board, 0.0) + weight
    return exposure
