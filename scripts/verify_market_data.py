"""A 股行情能力自检脚本。

两个数据源：
1. akshare（本地 Python，无 Key）—— 东方财富实时盘口，失败时回退腾讯逐笔源；
2. fuyao 同花顺金融数据 API（REST）—— 需要 HITHINK_FINANCE_API_KEY，
   优先从 %APPDATA%\\hithink-finance\\credentials.env 读取。

用法：
    python scripts/verify_market_data.py [600519]
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import akshare as ak
import requests

from market_data import em


FUYAO_BASE = "https://fuyao.aicubes.cn"


def load_hithink_api_key() -> str:
    key = os.environ.get("HITHINK_FINANCE_API_KEY", "").strip()
    if key:
        return key
    cred_file = Path(os.environ.get("APPDATA", "")) / "hithink-finance" / "credentials.env"
    if cred_file.exists():
        for line in cred_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("HITHINK_FINANCE_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("未找到 HITHINK_FINANCE_API_KEY（环境变量或 credentials.env）")


def eastmoney_snapshot(symbol: str) -> None:
    """东财实时线 -> 延迟线自动切换；完全不可用时回退腾讯逐笔（明确标注）。"""
    print(f"[eastmoney] {symbol} 行情")
    try:
        q = em.quote(symbol)
        mode = "延迟" if q.delayed else "实时"
        print(
            f"  {q.name} 最新 {q.price}  涨跌 {q.change}（{q.change_pct}%）"
            f"  [{mode}线 {q.host} / {q.transport}]"
        )
    except Exception as exc:
        print(f"  东财行情不可用（{type(exc).__name__}），回退腾讯逐笔源（非东财）…")
        df = ak.stock_zh_a_tick_tx_js(symbol=("sh" if symbol.startswith("6") else "sz") + symbol)
        print(f"  逐笔笔数 {len(df)}，最后一笔价格 {df.iloc[-1]['成交价格']}，时间 {df.iloc[-1]['成交时间']}")


def fuyao_snapshot(symbol: str) -> None:
    """同花顺 fuyao REST：先消歧得到 thscode，再取快照。"""
    key = load_hithink_api_key()
    headers = {"X-api-key": key}
    print(f"[fuyao] {symbol} 同花顺行情")

    resp = requests.get(
        f"{FUYAO_BASE}/api/meta/tickers/search",
        params={"q": symbol, "limit": 5},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    meta = resp.json()
    if meta.get("code") != 0 or not meta["data"]["item"]:
        raise RuntimeError(f"标的检索失败: {meta.get('message')}")
    thscode = meta["data"]["item"][0]["thscode"]
    name = meta["data"]["item"][0]["name"]

    resp = requests.get(
        f"{FUYAO_BASE}/api/a-share/prices/snapshot",
        params={"thscodes": thscode},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    snap = resp.json()
    if snap.get("code") != 0 or not snap["data"]["item"]:
        raise RuntimeError(f"快照失败: {snap.get('message')}")
    item = snap["data"]["item"][0]
    print(
        f"  {name} ({thscode}) 最新 {item['last_price']}  "
        f"涨跌 {item['price_change']}（{item['price_change_ratio_pct']}%）"
    )


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    symbol = sys.argv[1] if len(sys.argv) > 1 else "600519"
    eastmoney_snapshot(symbol)
    fuyao_snapshot(symbol)


if __name__ == "__main__":
    main()
