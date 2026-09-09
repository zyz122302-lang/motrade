"""本地 SQLite 存储层。只放公开市场数据（卡牌信息、历史卖价、赛制合法性缓存）。

用户个人持仓/买价记录不放在这里 —— 那些数据存在 Artifact 页面自己的私有数据库里
（见 dashboard.html），不落在本地仓库内，避免和"市场数据"混在一起被误提交。
"""

import sqlite3
from contextlib import contextmanager

from config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    mtgo_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    cardset TEXT,
    rarity TEXT,
    version TEXT,
    foil INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_prices (
    mtgo_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (mtgo_id, date)
);
CREATE INDEX IF NOT EXISTS idx_daily_prices_mtgo_id ON daily_prices(mtgo_id);

CREATE TABLE IF NOT EXISTS legality_cache (
    name_lower TEXT PRIMARY KEY,
    modern INTEGER NOT NULL DEFAULT 0,
    legacy INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_cards(conn, card_defs: dict):
    rows = [
        (
            int(mid),
            info.get("name"),
            info.get("cardset"),
            info.get("rarity"),
            info.get("version"),
            int(info.get("foil") or 0),
        )
        for mid, info in card_defs.items()
    ]
    conn.executemany(
        """INSERT INTO cards (mtgo_id, name, cardset, rarity, version, foil)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(mtgo_id) DO UPDATE SET
             name=excluded.name, cardset=excluded.cardset,
             rarity=excluded.rarity, version=excluded.version, foil=excluded.foil""",
        rows,
    )


def upsert_daily_prices(conn, date_str: str, prices: dict, only_ids: set | None = None):
    rows = []
    for mid, price in prices.items():
        mid_int = int(mid)
        if only_ids is not None and mid_int not in only_ids:
            continue
        rows.append((mid_int, date_str, price))
    if rows:
        conn.executemany(
            """INSERT INTO daily_prices (mtgo_id, date, price) VALUES (?, ?, ?)
               ON CONFLICT(mtgo_id, date) DO UPDATE SET price=excluded.price""",
            rows,
        )
    return len(rows)


def upsert_legality(conn, legality_by_name: dict, fetched_at: str):
    rows = [
        (name, int(v["modern"]), int(v["legacy"]), fetched_at)
        for name, v in legality_by_name.items()
    ]
    conn.executemany(
        """INSERT INTO legality_cache (name_lower, modern, legacy, fetched_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(name_lower) DO UPDATE SET
             modern=excluded.modern, legacy=excluded.legacy, fetched_at=excluded.fetched_at""",
        rows,
    )


def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key: str, value: str):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def price_history_for(conn, mtgo_id: int, limit_days: int = 400):
    rows = conn.execute(
        "SELECT date, price FROM daily_prices WHERE mtgo_id = ? ORDER BY date DESC LIMIT ?",
        (mtgo_id, limit_days),
    ).fetchall()
    return list(reversed(rows))  # oldest -> newest
