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

CREATE TABLE IF NOT EXISTS metagame_usage (
    name TEXT NOT NULL,
    format TEXT NOT NULL,
    week_of TEXT NOT NULL,
    deck_count INTEGER NOT NULL,
    sample_size INTEGER NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (name, format, week_of, source)
);
CREATE INDEX IF NOT EXISTS idx_metagame_usage_name ON metagame_usage(name, format);
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


def upsert_metagame_usage(conn, week_of: str, format_: str, source: str, usage: dict):
    """usage: {card_name: deck_count}。sample_size 是这一批统计的总样本套牌数，
    存进每一行方便算占比（deck_count / sample_size）。"""
    sample_size = usage.pop("__sample_size__", 0)
    rows = [
        (name, format_, week_of, count, sample_size, source)
        for name, count in usage.items()
    ]
    if rows:
        conn.executemany(
            """INSERT INTO metagame_usage (name, format, week_of, deck_count, sample_size, source)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(name, format, week_of, source) DO UPDATE SET
                 deck_count=excluded.deck_count, sample_size=excluded.sample_size""",
            rows,
        )
    return len(rows)


def metagame_usage_for_week(conn, week_of: str) -> dict:
    """把某一周刚写入的 metagame_usage 数据导出成
    {format: {"sample_size": N, "usage": {name: deck_count}}}，
    用于云端 routine 把这周的统计结果同步写进仪表盘 Artifact 的数据库
    （本地 SQLite 在云端每次运行都是全新的，不会跨次保留）。"""
    rows = conn.execute(
        "SELECT format, name, deck_count, sample_size FROM metagame_usage WHERE week_of = ?",
        (week_of,),
    ).fetchall()
    out = {}
    for fmt, name, deck_count, sample_size in rows:
        bucket = out.setdefault(fmt, {"sample_size": sample_size, "usage": {}})
        bucket["usage"][name] = deck_count
    return out


def import_metagame_usage_snapshot(conn, snapshot: dict, source: str = "mtgo_official"):
    """把从 Artifact `metagame_usage` 集合读回来的历史数据灌回本地 SQLite，
    snapshot 结构：{format: [{"week_of": ..., "sample_size": ..., "usage": {name: count}}, ...]}。
    每天流水线在算 formatUsage 之前先跑这一步，这样云端每次全新环境也能看到过去几周的数据。"""
    total = 0
    for fmt, weeks in snapshot.items():
        for week in weeks:
            usage = dict(week.get("usage") or {})
            usage["__sample_size__"] = week.get("sample_size", 0)
            total += upsert_metagame_usage(conn, week["week_of"], fmt, source, usage)
    return total


def metagame_usage_trend(conn, name: str, format_: str, limit_weeks: int = 30):
    """返回该卡在某赛制里最近几周的使用率序列：[(week_of, play_rate), ...]，
    合并同一周内多个 source（比如 MTGO 官方 + MTGTop8）的样本后再算占比。"""
    rows = conn.execute(
        """SELECT week_of, SUM(deck_count), SUM(sample_size) FROM metagame_usage
           WHERE name = ? AND format = ? GROUP BY week_of ORDER BY week_of DESC LIMIT ?""",
        (name, format_, limit_weeks),
    ).fetchall()
    out = [(w, (dc / ss) if ss else 0.0) for w, dc, ss in rows]
    return list(reversed(out))


def top_metagame_cards(conn, format_: str, week_of: str, min_play_rate: float = 0.0):
    """某赛制某一周里，使用率从高到低排的卡列表：[(name, play_rate), ...]。"""
    rows = conn.execute(
        """SELECT name, SUM(deck_count), SUM(sample_size) FROM metagame_usage
           WHERE format = ? AND week_of = ? GROUP BY name""",
        (format_, week_of),
    ).fetchall()
    out = [(name, dc / ss) for name, dc, ss in rows if ss]
    out = [x for x in out if x[1] >= min_play_rate]
    out.sort(key=lambda x: -x[1])
    return out


def price_history_for(conn, mtgo_id: int, limit_days: int = 400):
    rows = conn.execute(
        "SELECT date, price FROM daily_prices WHERE mtgo_id = ? ORDER BY date DESC LIMIT ?",
        (mtgo_id, limit_days),
    ).fetchall()
    return list(reversed(rows))  # oldest -> newest
