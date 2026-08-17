"""东方财富行情访问层（代理 / VPN 自适应）。

背景：
- akshare 的多数行情函数直连 push2*.eastmoney.com；
- 该域名对「VPN 出口」敏感：小鸡加速(ChickLite)等 VPN 开启时会被重置，且从
  Python/curl/系统栈任意客户端都连不上（与 TLS 指纹无关，是出口被风控）；
- 同花顺 fuyao 与腾讯源不受影响；东财的 datacenter-web、push2delay 域名
  在 VPN 开启时仍然可用。

本模块不改变 akshare 本身，只提供一条稳健的东财取数通道：
    实时线 push2.eastmoney.com  ->  延迟线 push2delay.eastmoney.com
每台主机再按「系统代理 -> 直连 -> 系统 curl」逐层回退，并标注实际使用的
主机与延迟属性。彻底解决 VPN 开关来回切换导致的时好时坏。
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests


REALTIME_HOST = "https://push2.eastmoney.com"
DELAYED_HOST = "https://push2delay.eastmoney.com"
# 历史 K 线只有 push2his 提供；部分 VPN 出口会风控该域名，失败时如实报错。
HISTORY_HOST = "https://push2his.eastmoney.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}


class EastMoneyUnavailable(RuntimeError):
    """所有东财行情通道均不可用。"""


@dataclass
class Quote:
    symbol: str
    name: str
    price: float
    change: float
    change_pct: float
    prev_close: float
    timestamp_sec: int | None
    host: str
    delayed: bool
    transport: str


def _system_proxy() -> str | None:
    """读取 Windows WinINET 系统代理（ChickLite 等会写在这里）。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        if not enabled or not server:
            return None
        if "=" in server:  # 分协议配置时取 https/http 入口
            mapping = dict(pair.split("=", 1) for pair in server.split(";") if "=" in pair)
            server = mapping.get("https", mapping.get("http", server))
        return server if "://" in server else f"http://{server}"
    except Exception:
        return None


def _curl_json(url: str, params: dict[str, Any], proxy: str | None = None) -> dict:
    cmd = ["curl.exe", "-sS", "--max-time", "12", "-G", url]
    for key, value in params.items():
        cmd += ["--data-urlencode", f"{key}={value}"]
    if proxy:
        cmd += ["-x", proxy]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise OSError(f"curl exit {proc.returncode}: {proc.stderr.strip()[:160]}")
    return json.loads(proc.stdout)


def fetch(url: str, params: dict[str, Any]) -> tuple[dict, str]:
    """按 系统代理 -> 直连 -> 系统 curl 的顺序取 JSON，返回 (data, transport)。"""
    attempts: list[tuple[str, Exception]] = []
    proxy = _system_proxy()

    transports: list[tuple[str, dict[str, Any]]] = []
    if proxy:
        transports.append(
            ("requests+proxy", {"proxies": {"http": proxy, "https": proxy}})
        )
    transports.append(("requests+direct", {"proxies": {"http": None, "https": None}}))

    for name, kwargs in transports:
        try:
            resp = requests.get(url, params=params, headers=_HEADERS, timeout=10, **kwargs)
            resp.raise_for_status()
            return resp.json(), name
        except Exception as exc:  # noqa: BLE001 - 逐层降级需要收集全部原因
            attempts.append((name, exc))

    for name, curl_proxy in [("curl+direct", None)] + (
        [("curl+proxy", proxy)] if proxy else []
    ):
        try:
            return _curl_json(url, params, curl_proxy), name
        except Exception as exc:  # noqa: BLE001
            attempts.append((name, exc))

    detail = "; ".join(f"{n}: {type(e).__name__}" for n, e in attempts)
    raise EastMoneyUnavailable(f"{url}?{urlencode(params)} -> {detail}")


def _secid(symbol: str) -> str:
    code = symbol.strip()
    if not (code.isdigit() and len(code) == 6):
        raise ValueError(f"需要 6 位证券代码，收到: {symbol!r}")
    market = "1" if code[0] in "569" else "0"  # 5/6/9 沪，0/1/2/3 深，北交所按 0 处理
    return f"{market}.{code}"


def quote(symbol: str) -> Quote:
    """单只 A 股最新行情：优先实时线，失败自动降级到延迟线。"""
    params = {
        "secid": _secid(symbol),
        "fields": "f43,f57,f58,f60,f86",
        "fltt": 2,
        "invt": 2,
    }
    last_error: Exception | None = None
    for host, delayed in [(REALTIME_HOST, False), (DELAYED_HOST, True)]:
        try:
            data, transport = fetch(f"{host}/api/qt/stock/get", params)
            payload = (data or {}).get("data") or {}
            if not payload or payload.get("f57") != symbol:
                raise EastMoneyUnavailable(f"返回缺少行情字段: {payload}")
            return Quote(
                symbol=symbol,
                name=payload.get("f58", ""),
                price=payload["f43"],
                change=round(payload["f43"] - payload["f60"], 3),
                change_pct=round((payload["f43"] - payload["f60"]) / payload["f60"] * 100, 3),
                prev_close=payload["f60"],
                timestamp_sec=payload.get("f86"),
                host=host,
                delayed=delayed,
                transport=transport,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise EastMoneyUnavailable(f"quote({symbol}) 全部通道失败: {last_error}")


def spot_top(count: int = 20, sort: str = "f3") -> list[dict[str, Any]]:
    """全市场涨幅榜样例（用于验证 clist 接口；完整分页请按接口自行扩展）。"""
    params = {
        "pn": 1,
        "pz": count,
        "po": 1,
        "np": 1,
        "fltt": 2,
        "invt": 2,
        "fid": sort,
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f12,f14,f2,f3,f6",
    }
    last_error: Exception | None = None
    for host, delayed in [(REALTIME_HOST, False), (DELAYED_HOST, True)]:
        try:
            data, transport = fetch(f"{host}/api/qt/clist/get", params)
            payload = (data or {}).get("data") or {}
            rows = []
            for item in payload.get("diff") or []:
                rows.append(
                    {
                        "symbol": item.get("f12"),
                        "name": item.get("f14"),
                        "price": item.get("f2"),
                        "change_pct": item.get("f3"),
                        "turnover": item.get("f6"),
                        "delayed": delayed,
                        "transport": transport,
                    }
                )
            if rows:
                return rows
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise EastMoneyUnavailable(f"spot_top 全部通道失败: {last_error}")


def diagnose() -> None:
    """打印东财各主机的连通性矩阵，便于判断 VPN/代理影响。"""
    print("东财连通性诊断（当前网络环境）:")
    for host, label in [
        ("push2.eastmoney.com", "实时行情线"),
        ("push2delay.eastmoney.com", "延迟行情线"),
        ("datacenter-web.eastmoney.com", "数据中心"),
    ]:
        params = (
            {"secid": "1.600519", "fields": "f43", "fltt": 2, "invt": 2}
            if "push2" in host
            else {"reportName": "RPT_F10_BASIC_ORGINFO", "columns": "ALL", "filter": '(SECUCODE="600519.SH")'}
        )
        path = "/api/qt/stock/get" if "push2" in host else "/api/data/v1/get"
        try:
            _, transport = fetch(f"https://{host}{path}", params)
            print(f"  [OK] {label:8s} {host} (via {transport})")
        except EastMoneyUnavailable as exc:
            print(f"  [--] {label:8s} {host} 不可用 ({exc})")


def _num(value: Any) -> float | None:
    """把东财字段里的 `-` / 空串 / None 统一转成 None，其余转 float。"""
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_board_code(code: str) -> str:
    """把 BKxxxx / 90.BKxxxx 等写法统一成 BKxxxx。"""
    code = str(code).strip().upper()
    if code.startswith("90."):
        code = code[3:]
    if not code.startswith("BK"):
        code = f"BK{code}"
    return code


def _clist(
    fs: str,
    fields: str,
    *,
    fid: str = "f12",
    page_size: int = 100,
) -> list[dict[str, Any]]:
    """分页拉取东财 clist 接口；实时线失败自动换延迟线。"""
    failures: list[str] = []
    for host, delayed in [(REALTIME_HOST, False), (DELAYED_HOST, True)]:
        try:
            rows: list[dict[str, Any]] = []
            page = 1
            while True:
                params = {
                    "pn": page,
                    "pz": page_size,
                    "po": 1,
                    "np": 1,
                    "fltt": 2,
                    "invt": 2,
                    "fid": fid,
                    "fs": fs,
                    "fields": fields,
                }
                data, transport = fetch(f"{host}/api/qt/clist/get", params)
                payload = (data or {}).get("data")
                if not payload:
                    raise EastMoneyUnavailable("clist 返回缺少 data")
                total = int(payload.get("total") or 0)
                diff = payload.get("diff") or []
                for item in diff:
                    row = dict(item)
                    row["_delayed"] = delayed
                    row["_transport"] = transport
                    rows.append(row)
                if not diff or (total and len(rows) >= total):
                    break
                page += 1
            return rows
        except Exception as exc:  # noqa: BLE001 - 逐层降级需要收集全部原因
            failures.append(f"{host}: {type(exc).__name__}")
    raise EastMoneyUnavailable(f"clist fs={fs} -> {'; '.join(failures)}")


def boards(kind: str) -> list[dict[str, Any]]:
    """东财行业/概念板块列表。

    kind 只接受 `industry`（行业板块）或 `concept`（概念板块）。
    返回字段：code/name/price/change_pct/volume/amount/turnover_rate/
    delayed/transport。
    """
    fs = {"industry": "m:90+t:2+f:!50", "concept": "m:90+t:3+f:!50"}.get(kind)
    if fs is None:
        raise ValueError(f"kind 只支持 industry/concept，收到: {kind!r}")
    rows = _clist(fs, "f12,f14,f2,f3,f5,f6,f8")
    return [
        {
            "code": row.get("f12"),
            "name": row.get("f14"),
            "price": _num(row.get("f2")),
            "change_pct": _num(row.get("f3")),
            "volume": _num(row.get("f5")),
            "amount": _num(row.get("f6")),
            "turnover_rate": _num(row.get("f8")),
            "delayed": row.get("_delayed"),
            "transport": row.get("_transport"),
        }
        for row in rows
    ]


def board_history(
    code: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """东财板块日线历史（前复权）。

    code 接受 BKxxxx 或 90.BKxxxx；start/end 为 YYYY-MM-DD。
    返回字段：date/open/close/high/low/volume/amount/amplitude/
    change_pct/change/turnover_rate。
    """
    board_code = _normalize_board_code(code)
    params = {
        "secid": f"90.{board_code}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "beg": start_date.replace("-", ""),
        "end": end_date.replace("-", ""),
    }
    failures: list[str] = []
    for attempt in range(2):
        try:
            data, transport = fetch(f"{HISTORY_HOST}/api/qt/stock/kline/get", params)
            payload = (data or {}).get("data")
            if not payload:
                raise EastMoneyUnavailable(f"板块历史返回缺少 data: {board_code}")
            rows = []
            for line in payload.get("klines") or []:
                parts = line.split(",")
                if len(parts) < 11:
                    continue
                rows.append(
                    {
                        "date": parts[0],
                        "open": _num(parts[1]),
                        "close": _num(parts[2]),
                        "high": _num(parts[3]),
                        "low": _num(parts[4]),
                        "volume": _num(parts[5]),
                        "amount": _num(parts[6]),
                        "amplitude": _num(parts[7]),
                        "change_pct": _num(parts[8]),
                        "change": _num(parts[9]),
                        "turnover_rate": _num(parts[10]),
                        "transport": transport,
                    }
                )
            return rows
        except Exception as exc:  # noqa: BLE001 - 重试需要收集原因
            failures.append(f"attempt{attempt}: {type(exc).__name__}")
            time.sleep(1)
    raise EastMoneyUnavailable(f"板块历史 {board_code} -> {'; '.join(failures)}")


def stock_history(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    adjust: str = "forward",
) -> list[dict[str, Any]]:
    """单只 A 股日线历史，作为同花顺本地库不可用时的东财回退通道。

    adjust 取值：forward / backward / none；对应东财 fqt=1 / 2 / 0。
    返回字段与 board_history 一致，另加 symbol。
    """
    code = str(symbol).strip()
    if not (code.isdigit() and len(code) == 6):
        raise ValueError(f"股票代码需要是 6 位数字，收到: {symbol!r}")
    fqt = {"forward": 1, "backward": 2, "none": 0}.get(adjust)
    if fqt is None:
        raise ValueError(f"adjust 只支持 forward/backward/none，收到: {adjust!r}")

    params = {
        "secid": _secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": str(fqt),
        "beg": start_date.replace("-", ""),
        "end": end_date.replace("-", ""),
    }
    failures: list[str] = []
    for attempt in range(2):
        try:
            data, transport = fetch(f"{HISTORY_HOST}/api/qt/stock/kline/get", params)
            payload = (data or {}).get("data")
            if not payload:
                raise EastMoneyUnavailable(f"个股历史返回缺少 data: {code}")
            rows = []
            for line in payload.get("klines") or []:
                parts = line.split(",")
                if len(parts) < 11:
                    continue
                rows.append(
                    {
                        "symbol": code,
                        "date": parts[0],
                        "open": _num(parts[1]),
                        "close": _num(parts[2]),
                        "high": _num(parts[3]),
                        "low": _num(parts[4]),
                        "volume": _num(parts[5]),
                        "amount": _num(parts[6]),
                        "amplitude": _num(parts[7]),
                        "change_pct": _num(parts[8]),
                        "change": _num(parts[9]),
                        "turnover_rate": _num(parts[10]),
                        "transport": transport,
                    }
                )
            return rows
        except Exception as exc:  # noqa: BLE001 - 重试需要收集原因
            failures.append(f"attempt{attempt}: {type(exc).__name__}")
            time.sleep(1)
    raise EastMoneyUnavailable(f"个股历史 {code} -> {'; '.join(failures)}")


def board_constituents(code: str) -> list[dict[str, Any]]:
    """东财板块成分股。

    返回字段：symbol/name/price/change_pct/volume/amount/turnover_rate/
    total_market_cap/float_market_cap。
    """
    board_code = _normalize_board_code(code)
    rows = _clist(f"b:{board_code}+f:!50", "f12,f14,f2,f3,f5,f6,f8,f20,f21", fid="f3")
    return [
        {
            "symbol": row.get("f12"),
            "name": row.get("f14"),
            "price": _num(row.get("f2")),
            "change_pct": _num(row.get("f3")),
            "volume": _num(row.get("f5")),
            "amount": _num(row.get("f6")),
            "turnover_rate": _num(row.get("f8")),
            "total_market_cap": _num(row.get("f20")),
            "float_market_cap": _num(row.get("f21")),
            "delayed": row.get("_delayed"),
            "transport": row.get("_transport"),
        }
        for row in rows
    ]


def stock_profile(symbol: str) -> dict[str, Any]:
    """单只 A 股补充信息：市值、股本与换手率。"""
    params = {
        "secid": _secid(symbol),
        "fields": "f43,f47,f48,f57,f58,f84,f85,f116,f117,f168",
        "fltt": 2,
        "invt": 2,
    }
    last_error: Exception | None = None
    for host, delayed in [(REALTIME_HOST, False), (DELAYED_HOST, True)]:
        try:
            data, transport = fetch(f"{host}/api/qt/stock/get", params)
            payload = (data or {}).get("data") or {}
            if not payload or payload.get("f57") != symbol:
                raise EastMoneyUnavailable(f"返回缺少个股信息字段: {symbol}")
            return {
                "symbol": payload.get("f57"),
                "name": payload.get("f58"),
                "price": _num(payload.get("f43")),
                "volume": _num(payload.get("f47")),
                "amount": _num(payload.get("f48")),
                "total_shares": _num(payload.get("f84")),
                "float_shares": _num(payload.get("f85")),
                "total_market_cap": _num(payload.get("f116")),
                "float_market_cap": _num(payload.get("f117")),
                "turnover_rate": _num(payload.get("f168")),
                "delayed": delayed,
                "transport": transport,
            }
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise EastMoneyUnavailable(f"stock_profile({symbol}) 全部通道失败: {last_error}")
