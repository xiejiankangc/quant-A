"""本地规范数据仓库（DuckDB）。

职责：把原始抓取结果转成带 `trade_date` / `as_of` / `source` 的可重算表，
并提供幂等 upsert。所有时间字段统一存为 `YYYY-MM-DD` 字符串，数值缺失用 NULL。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import duckdb
import pandas as pd


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS index_daily (
    trade_date VARCHAR NOT NULL,
    thscode VARCHAR NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume DOUBLE,
    amount DOUBLE,
    source VARCHAR NOT NULL,
    as_of VARCHAR NOT NULL,
    ingest_time VARCHAR NOT NULL,
    PRIMARY KEY (trade_date, thscode, source)
);

CREATE TABLE IF NOT EXISTS index_snapshot (
    as_of VARCHAR NOT NULL,
    thscode VARCHAR NOT NULL,
    last_price DOUBLE,
    change_pct DOUBLE,
    volume DOUBLE,
    amount DOUBLE,
    source VARCHAR NOT NULL,
    ingest_time VARCHAR NOT NULL,
    PRIMARY KEY (as_of, thscode, source)
);

CREATE TABLE IF NOT EXISTS stock_daily (
    trade_date VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume DOUBLE,
    turnover_rate_pct DOUBLE,
    amount DOUBLE,
    source VARCHAR NOT NULL,
    adjust VARCHAR,
    as_of VARCHAR NOT NULL,
    ingest_time VARCHAR NOT NULL,
    PRIMARY KEY (trade_date, symbol, source)
);

CREATE TABLE IF NOT EXISTS stock_snapshot (
    as_of VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    name VARCHAR,
    last_price DOUBLE,
    change_pct DOUBLE,
    volume DOUBLE,
    amount DOUBLE,
    turnover_rate_pct DOUBLE,
    total_market_cap DOUBLE,
    float_market_cap DOUBLE,
    total_shares DOUBLE,
    float_shares DOUBLE,
    delayed BOOLEAN,
    source VARCHAR NOT NULL,
    ingest_time VARCHAR NOT NULL,
    PRIMARY KEY (as_of, symbol, source)
);

CREATE TABLE IF NOT EXISTS board_meta (
    thscode VARCHAR PRIMARY KEY,
    kind VARCHAR NOT NULL,
    name VARCHAR,
    source VARCHAR NOT NULL,
    ingest_time VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS board_daily (
    trade_date VARCHAR NOT NULL,
    thscode VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    close DOUBLE,
    change_pct DOUBLE,
    volume DOUBLE,
    amount DOUBLE,
    turnover_rate_pct DOUBLE,
    source VARCHAR NOT NULL,
    as_of VARCHAR NOT NULL,
    ingest_time VARCHAR NOT NULL,
    PRIMARY KEY (trade_date, thscode, source)
);

CREATE TABLE IF NOT EXISTS board_constituent_daily (
    as_of VARCHAR NOT NULL,
    board_thscode VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    name VARCHAR,
    last_price DOUBLE,
    change_pct DOUBLE,
    volume DOUBLE,
    amount DOUBLE,
    turnover_rate_pct DOUBLE,
    total_market_cap DOUBLE,
    float_market_cap DOUBLE,
    source VARCHAR NOT NULL,
    ingest_time VARCHAR NOT NULL,
    PRIMARY KEY (as_of, board_thscode, symbol, source)
);

CREATE TABLE IF NOT EXISTS market_breadth_daily (
    as_of VARCHAR NOT NULL,
    total INTEGER,
    up_count INTEGER,
    down_count INTEGER,
    flat_count INTEGER,
    limit_up_count INTEGER,
    limit_down_count INTEGER,
    limit_break_count INTEGER,
    ladder_height INTEGER,
    source VARCHAR NOT NULL,
    ingest_time VARCHAR NOT NULL,
    PRIMARY KEY (as_of, source)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    key VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    rows INTEGER,
    message VARCHAR,
    fetched_at VARCHAR NOT NULL,
    PRIMARY KEY (key, fetched_at)
);
"""


class Store:
    """DuckDB 规范仓库访问入口。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.path))

    def init_schema(self) -> None:
        with self.connect() as con:
            con.execute(SCHEMA_SQL)
            con.commit()

    def write_rows(
        self,
        table: str,
        rows: Iterable[dict[str, Any]],
        *,
        columns: list[str],
    ) -> int:
        """把 dict 行按指定列顺序 upsert 进表；主键冲突时覆盖。"""
        frame = pd.DataFrame(list(rows), columns=columns)
        if frame.empty:
            return 0
        with self.connect() as con:
            con.register("incoming_rows", frame)
            con.execute(
                f"INSERT OR REPLACE INTO {table} SELECT * FROM incoming_rows"
            )
            con.unregister("incoming_rows")
            con.commit()
        return int(len(frame))

    def query_df(self, sql: str) -> pd.DataFrame:
        with self.connect() as con:
            return con.execute(sql).fetch_df()

    def table_count(self, table: str) -> int:
        with self.connect() as con:
            row = con.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
        return int(row[0]) if row else 0

    def log(self, key: str, status: str, *, rows: int = 0, message: str = "", fetched_at: str) -> None:
        self.write_rows(
            "fetch_log",
            [
                {
                    "key": key,
                    "status": status,
                    "rows": rows,
                    "message": message[:2000] if message else None,
                    "fetched_at": fetched_at,
                }
            ],
            columns=["key", "status", "rows", "message", "fetched_at"],
        )
