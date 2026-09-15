"""Хранилище бота. Всё лежит в data/bot.db (SQLite), переживает перезапуски."""
import aiosqlite
import os

# DATA_DIR — папка для базы. На хостинге укажи путь к постоянному диску (например /data), дома можно не трогать.
DATA_DIR = os.getenv("DATA_DIR") or os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id        INTEGER PRIMARY KEY,
    invite_channel  INTEGER,
    count_channel   INTEGER,
    streak_channel  INTEGER,
    timezone        TEXT DEFAULT 'Europe/Moscow',
    words_channel   INTEGER,
    rep_channel     INTEGER,
    welcome_channel INTEGER,
    welcome_text    TEXT,
    projects_channel INTEGER,
    projects_msg    INTEGER,
    streak_last_msg INTEGER
);
CREATE TABLE IF NOT EXISTS snake_scores (
    guild_id INTEGER, user_id INTEGER, best INTEGER DEFAULT 0, games INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS snake_duels (
    guild_id INTEGER, user_id INTEGER, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS project_roles (
    guild_id INTEGER, emoji TEXT, role_id INTEGER, name TEXT,
    PRIMARY KEY (guild_id, emoji)
);
CREATE TABLE IF NOT EXISTS invite_points (
    guild_id INTEGER, user_id INTEGER, points INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS invite_log (
    guild_id INTEGER, inviter_id INTEGER, invited_id INTEGER, code TEXT, ts INTEGER
);
CREATE TABLE IF NOT EXISTS counting (
    guild_id INTEGER PRIMARY KEY,
    current  INTEGER DEFAULT 0,
    last_user INTEGER,
    last_msg  INTEGER,
    record    INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS words_state (
    guild_id INTEGER PRIMARY KEY,
    last_word TEXT, last_user INTEGER, last_msg INTEGER,
    chain INTEGER DEFAULT 0, record INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS words_used (
    guild_id INTEGER, word TEXT, PRIMARY KEY (guild_id, word)
);
CREATE TABLE IF NOT EXISTS words_score (
    guild_id INTEGER, user_id INTEGER, words INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS words_custom (
    guild_id INTEGER, word TEXT, allowed INTEGER, PRIMARY KEY (guild_id, word)
);
CREATE TABLE IF NOT EXISTS rep_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER, giver_id INTEGER, target_id INTEGER,
    delta INTEGER, reason TEXT, ts INTEGER
);
CREATE TABLE IF NOT EXISTS count_score (
    guild_id INTEGER, user_id INTEGER, numbers INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS streaks (
    guild_id INTEGER, user_id INTEGER,
    streak   INTEGER DEFAULT 0,
    best     INTEGER DEFAULT 0,
    last_day TEXT,
    PRIMARY KEY (guild_id, user_id)
);
"""


CONFIG_KEYS = ("invite_channel", "count_channel", "streak_channel", "timezone", "words_channel", "rep_channel",
               "welcome_channel", "welcome_text", "projects_channel", "projects_msg", "streak_last_msg")

# колонки, которые появлялись в новых версиях — добавляем в старые базы
MIGRATIONS = {
    "words_channel": "INTEGER", "rep_channel": "INTEGER", "welcome_channel": "INTEGER", "welcome_text": "TEXT",
    "projects_channel": "INTEGER", "projects_msg": "INTEGER", "streak_last_msg": "INTEGER",
}


class DB:
    def __init__(self):
        self.conn: aiosqlite.Connection | None = None
        self._cfg: dict[int, dict] = {}   # кэш настроек: on_message дёргает их на каждое сообщение

    async def open(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self.conn = await aiosqlite.connect(DB_PATH)
        self.conn.row_factory = aiosqlite.Row
        # WAL: база не бьётся при внезапном выключении, читатели не ждут писателей
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.executescript(SCHEMA)
        cur = await self.conn.execute("PRAGMA table_info(guild_config)")
        cols = {r[1] for r in await cur.fetchall()}
        for col, typ in MIGRATIONS.items():
            if col not in cols:
                await self.conn.execute(f"ALTER TABLE guild_config ADD COLUMN {col} {typ}")
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    # ---------- config ----------
    async def get_config(self, guild_id: int) -> dict:
        c = self._cfg.get(guild_id)
        if c is not None:
            return c
        cur = await self.conn.execute("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,))
        row = await cur.fetchone()
        if row is None:
            await self.conn.execute("INSERT OR IGNORE INTO guild_config(guild_id) VALUES (?)", (guild_id,))
            await self.conn.commit()
            cur = await self.conn.execute("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,))
            row = await cur.fetchone()
        c = dict(row)
        for k in CONFIG_KEYS:            # на всякий случай: все ключи всегда есть
            c.setdefault(k, None)
        if not c.get("timezone"):
            c["timezone"] = "Europe/Moscow"
        self._cfg[guild_id] = c
        return c

    async def set_config(self, guild_id: int, key: str, value):
        if key not in CONFIG_KEYS:
            raise ValueError(f"неизвестная настройка {key}")
        await self.get_config(guild_id)
        await self.conn.execute(f"UPDATE guild_config SET {key}=? WHERE guild_id=?", (value, guild_id))
        await self.conn.commit()
        self._cfg.pop(guild_id, None)

    async def all_configs(self):
        cur = await self.conn.execute("SELECT guild_id FROM guild_config")
        return [r["guild_id"] for r in await cur.fetchall()]

    # ---------- invites ----------
    async def add_point(self, guild_id: int, user_id: int, delta: int = 1) -> int:
        await self.conn.execute(
            "INSERT INTO invite_points(guild_id,user_id,points) VALUES(?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET points=points+excluded.points",
            (guild_id, user_id, delta))
        await self.conn.commit()
        return await self.get_points(guild_id, user_id)

    async def get_points(self, guild_id: int, user_id: int) -> int:
        cur = await self.conn.execute("SELECT points FROM invite_points WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        row = await cur.fetchone()
        return row["points"] if row else 0

    async def top_points(self, guild_id: int, limit: int = 10):
        cur = await self.conn.execute(
            "SELECT user_id, points FROM invite_points WHERE guild_id=? AND points>0 ORDER BY points DESC LIMIT ?",
            (guild_id, limit))
        return await cur.fetchall()

    async def log_invite(self, guild_id, inviter_id, invited_id, code, ts):
        await self.conn.execute("INSERT INTO invite_log VALUES(?,?,?,?,?)", (guild_id, inviter_id, invited_id, code, ts))
        await self.conn.commit()

    async def was_invited_before(self, guild_id: int, invited_id: int) -> bool:
        cur = await self.conn.execute("SELECT 1 FROM invite_log WHERE guild_id=? AND invited_id=? LIMIT 1", (guild_id, invited_id))
        return await cur.fetchone() is not None

    # ---------- counting ----------
    async def get_count(self, guild_id: int) -> dict:
        cur = await self.conn.execute("SELECT * FROM counting WHERE guild_id=?", (guild_id,))
        row = await cur.fetchone()
        if row is None:
            await self.conn.execute("INSERT INTO counting(guild_id) VALUES(?)", (guild_id,))
            await self.conn.commit()
            return {"guild_id": guild_id, "current": 0, "last_user": None, "last_msg": None, "record": 0}
        return dict(row)

    async def set_count(self, guild_id: int, current: int, last_user, last_msg):
        st = await self.get_count(guild_id)
        record = max(st["record"], current)
        await self.conn.execute(
            "UPDATE counting SET current=?, last_user=?, last_msg=?, record=? WHERE guild_id=?",
            (current, last_user, last_msg, record, guild_id))
        await self.conn.commit()

    # ---------- streaks ----------
    async def get_streak(self, guild_id: int, user_id: int) -> dict:
        cur = await self.conn.execute("SELECT * FROM streaks WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        row = await cur.fetchone()
        return dict(row) if row else {"guild_id": guild_id, "user_id": user_id, "streak": 0, "best": 0, "last_day": None}

    async def set_streak(self, guild_id: int, user_id: int, streak: int, last_day: str | None):
        st = await self.get_streak(guild_id, user_id)
        best = max(st["best"], streak)
        await self.conn.execute(
            "INSERT INTO streaks(guild_id,user_id,streak,best,last_day) VALUES(?,?,?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET streak=excluded.streak, best=excluded.best, last_day=excluded.last_day",
            (guild_id, user_id, streak, best, last_day))
        await self.conn.commit()

    async def top_streaks(self, guild_id: int, limit: int = 10):
        cur = await self.conn.execute(
            "SELECT user_id, streak, best FROM streaks WHERE guild_id=? AND streak>0 ORDER BY streak DESC LIMIT ?",
            (guild_id, limit))
        return await cur.fetchall()

    async def active_streaks(self, guild_id: int):
        cur = await self.conn.execute("SELECT user_id, streak, last_day FROM streaks WHERE guild_id=? AND streak>0", (guild_id,))
        return await cur.fetchall()

    async def all_guilds_with_streak_channel(self):
        cur = await self.conn.execute("SELECT guild_id, streak_channel, timezone FROM guild_config WHERE streak_channel IS NOT NULL")
        return await cur.fetchall()

    # ---------- words ----------
    async def get_words(self, guild_id: int) -> dict:
        cur = await self.conn.execute("SELECT * FROM words_state WHERE guild_id=?", (guild_id,))
        row = await cur.fetchone()
        if row is None:
            await self.conn.execute("INSERT INTO words_state(guild_id) VALUES(?)", (guild_id,))
            await self.conn.commit()
            return {"guild_id": guild_id, "last_word": None, "last_user": None, "last_msg": None, "chain": 0, "record": 0}
        return dict(row)

    async def push_word(self, guild_id: int, word: str, user_id: int, msg_id: int):
        st = await self.get_words(guild_id)
        chain = st["chain"] + 1
        await self.conn.execute("UPDATE words_state SET last_word=?, last_user=?, last_msg=?, chain=?, record=? WHERE guild_id=?",
                                (word, user_id, msg_id, chain, max(st["record"], chain), guild_id))
        await self.conn.execute("INSERT OR IGNORE INTO words_used VALUES(?,?)", (guild_id, word))
        await self.conn.execute("INSERT INTO words_score(guild_id,user_id,words) VALUES(?,?,1) "
                                "ON CONFLICT(guild_id,user_id) DO UPDATE SET words=words+1", (guild_id, user_id))
        await self.conn.commit()
        return chain

    async def word_used(self, guild_id: int, word: str) -> bool:
        cur = await self.conn.execute("SELECT 1 FROM words_used WHERE guild_id=? AND word=?", (guild_id, word))
        return await cur.fetchone() is not None

    async def reset_words(self, guild_id: int):
        st = await self.get_words(guild_id)
        await self.conn.execute("UPDATE words_state SET last_word=NULL, last_user=NULL, last_msg=NULL, chain=0 WHERE guild_id=?", (guild_id,))
        await self.conn.execute("DELETE FROM words_used WHERE guild_id=?", (guild_id,))
        await self.conn.commit()
        return st

    async def top_words(self, guild_id: int, limit: int = 10):
        cur = await self.conn.execute("SELECT user_id, words FROM words_score WHERE guild_id=? ORDER BY words DESC LIMIT ?", (guild_id, limit))
        return await cur.fetchall()

    async def custom_word(self, guild_id: int, word: str):
        """None — нет записи; True — разрешено вручную; False — запрещено вручную."""
        cur = await self.conn.execute("SELECT allowed FROM words_custom WHERE guild_id=? AND word=?", (guild_id, word))
        row = await cur.fetchone()
        return None if row is None else bool(row["allowed"])

    async def set_custom_word(self, guild_id: int, word: str, allowed: bool):
        await self.conn.execute("INSERT OR REPLACE INTO words_custom VALUES(?,?,?)", (guild_id, word, int(allowed)))
        await self.conn.commit()

    # ---------- reputation ----------
    async def rep_last_from(self, guild_id: int, giver_id: int, target_id: int):
        cur = await self.conn.execute(
            "SELECT ts FROM rep_log WHERE guild_id=? AND giver_id=? AND target_id=? ORDER BY ts DESC LIMIT 1",
            (guild_id, giver_id, target_id))
        row = await cur.fetchone()
        return row["ts"] if row else None

    async def rep_add(self, guild_id, giver_id, target_id, delta, reason, ts):
        await self.conn.execute("INSERT INTO rep_log(guild_id,giver_id,target_id,delta,reason,ts) VALUES(?,?,?,?,?,?)",
                                (guild_id, giver_id, target_id, delta, reason, ts))
        await self.conn.commit()

    async def rep_summary(self, guild_id: int, user_id: int) -> dict:
        cur = await self.conn.execute(
            "SELECT COALESCE(SUM(delta),0) AS total, "
            "COALESCE(SUM(CASE WHEN delta>0 THEN 1 ELSE 0 END),0) AS plus, "
            "COALESCE(SUM(CASE WHEN delta<0 THEN 1 ELSE 0 END),0) AS minus "
            "FROM rep_log WHERE guild_id=? AND target_id=?", (guild_id, user_id))
        row = await cur.fetchone()
        cur = await self.conn.execute("SELECT COUNT(*) AS n FROM rep_log WHERE guild_id=? AND giver_id=?", (guild_id, user_id))
        given = (await cur.fetchone())["n"]
        cur = await self.conn.execute(
            "SELECT giver_id, delta, reason, ts FROM rep_log WHERE guild_id=? AND target_id=? ORDER BY ts DESC LIMIT 5",
            (guild_id, user_id))
        recent = await cur.fetchall()
        return {"total": row["total"], "plus": row["plus"], "minus": row["minus"], "given": given, "recent": recent}

    async def rep_rank(self, guild_id: int, user_id: int) -> int | None:
        cur = await self.conn.execute(
            "SELECT target_id, SUM(delta) AS t FROM rep_log WHERE guild_id=? GROUP BY target_id ORDER BY t DESC", (guild_id,))
        rows = await cur.fetchall()
        for i, r in enumerate(rows, 1):
            if r["target_id"] == user_id:
                return i
        return None

    async def rep_top(self, guild_id: int, limit: int = 10):
        cur = await self.conn.execute(
            "SELECT target_id, SUM(delta) AS t, COUNT(*) AS n FROM rep_log WHERE guild_id=? GROUP BY target_id ORDER BY t DESC LIMIT ?",
            (guild_id, limit))
        return await cur.fetchall()

    async def rep_clear(self, guild_id: int, user_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM rep_log WHERE guild_id=? AND target_id=?", (guild_id, user_id))
        await self.conn.commit()
        return cur.rowcount

    # ---------- snake ----------
    async def snake_result(self, guild_id: int, user_id: int, score: int) -> tuple[int, bool]:
        cur = await self.conn.execute("SELECT best FROM snake_scores WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        row = await cur.fetchone()
        best = row["best"] if row else 0
        new_best = score > best
        await self.conn.execute(
            "INSERT INTO snake_scores(guild_id,user_id,best,games) VALUES(?,?,?,1) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET best=MAX(best, excluded.best), games=games+1",
            (guild_id, user_id, score))
        await self.conn.commit()
        return max(best, score), new_best

    async def snake_top(self, guild_id: int, limit: int = 10):
        cur = await self.conn.execute("SELECT user_id, best, games FROM snake_scores WHERE guild_id=? AND best>0 ORDER BY best DESC LIMIT ?", (guild_id, limit))
        return await cur.fetchall()

    # ---------- project roles ----------
    async def project_roles(self, guild_id: int):
        cur = await self.conn.execute("SELECT emoji, role_id, name FROM project_roles WHERE guild_id=? ORDER BY rowid", (guild_id,))
        return await cur.fetchall()

    async def project_set(self, guild_id: int, emoji: str, role_id: int, name: str):
        await self.conn.execute("INSERT OR REPLACE INTO project_roles VALUES(?,?,?,?)", (guild_id, emoji, role_id, name))
        await self.conn.commit()

    async def project_del(self, guild_id: int, emoji: str) -> bool:
        cur = await self.conn.execute("DELETE FROM project_roles WHERE guild_id=? AND emoji=?", (guild_id, emoji))
        await self.conn.commit()
        return cur.rowcount > 0

    # ---------- профиль: место игрока в каждом топе ----------
    async def _rank(self, sql: str, params: tuple, user_id: int) -> tuple[int | None, int]:
        """(место или None, всего участников). sql должен возвращать user_id в нужном порядке."""
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        for i, r in enumerate(rows, 1):
            if r[0] == user_id:
                return i, len(rows)
        return None, len(rows)

    async def rank_points(self, guild_id: int, user_id: int):
        return await self._rank("SELECT user_id FROM invite_points WHERE guild_id=? AND points>0 ORDER BY points DESC", (guild_id,), user_id)

    async def rank_streak(self, guild_id: int, user_id: int):
        return await self._rank("SELECT user_id FROM streaks WHERE guild_id=? AND streak>0 ORDER BY streak DESC", (guild_id,), user_id)

    async def rank_best_streak(self, guild_id: int, user_id: int):
        return await self._rank("SELECT user_id FROM streaks WHERE guild_id=? AND best>0 ORDER BY best DESC", (guild_id,), user_id)

    async def rank_words(self, guild_id: int, user_id: int):
        return await self._rank("SELECT user_id FROM words_score WHERE guild_id=? AND words>0 ORDER BY words DESC", (guild_id,), user_id)

    async def rank_snake(self, guild_id: int, user_id: int):
        return await self._rank("SELECT user_id FROM snake_scores WHERE guild_id=? AND best>0 ORDER BY best DESC", (guild_id,), user_id)

    async def rank_rep(self, guild_id: int, user_id: int):
        return await self._rank("SELECT target_id FROM rep_log WHERE guild_id=? GROUP BY target_id ORDER BY SUM(delta) DESC", (guild_id,), user_id)

    async def words_count(self, guild_id: int, user_id: int) -> int:
        cur = await self.conn.execute("SELECT words FROM words_score WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        row = await cur.fetchone()
        return row["words"] if row else 0

    async def snake_stats(self, guild_id: int, user_id: int) -> tuple[int, int]:
        cur = await self.conn.execute("SELECT best, games FROM snake_scores WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        row = await cur.fetchone()
        return (row["best"], row["games"]) if row else (0, 0)

    async def count_numbers(self, guild_id: int, user_id: int) -> int:
        cur = await self.conn.execute("SELECT numbers FROM count_score WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        row = await cur.fetchone()
        return row["numbers"] if row else 0

    async def rank_count(self, guild_id: int, user_id: int):
        return await self._rank("SELECT user_id FROM count_score WHERE guild_id=? AND numbers>0 ORDER BY numbers DESC", (guild_id,), user_id)

    async def add_count_number(self, guild_id: int, user_id: int):
        await self.conn.execute("INSERT INTO count_score(guild_id,user_id,numbers) VALUES(?,?,1) "
                                "ON CONFLICT(guild_id,user_id) DO UPDATE SET numbers=numbers+1", (guild_id, user_id))

    async def invited_count(self, guild_id: int, user_id: int) -> int:
        cur = await self.conn.execute("SELECT COUNT(DISTINCT invited_id) AS n FROM invite_log WHERE guild_id=? AND inviter_id=?", (guild_id, user_id))
        return (await cur.fetchone())["n"]

    # ---------- дуэли змейки ----------
    async def duel_result(self, guild_id: int, winner_id: int, loser_id: int):
        await self.conn.execute("INSERT INTO snake_duels(guild_id,user_id,wins,losses) VALUES(?,?,1,0) "
                                "ON CONFLICT(guild_id,user_id) DO UPDATE SET wins=wins+1", (guild_id, winner_id))
        await self.conn.execute("INSERT INTO snake_duels(guild_id,user_id,wins,losses) VALUES(?,?,0,1) "
                                "ON CONFLICT(guild_id,user_id) DO UPDATE SET losses=losses+1", (guild_id, loser_id))
        await self.conn.commit()

    async def duel_top(self, guild_id: int, limit: int = 10):
        cur = await self.conn.execute("SELECT user_id, wins, losses FROM snake_duels WHERE guild_id=? AND wins>0 ORDER BY wins DESC, losses ASC LIMIT ?", (guild_id, limit))
        return await cur.fetchall()

    async def duel_stats(self, guild_id: int, user_id: int) -> tuple[int, int]:
        cur = await self.conn.execute("SELECT wins, losses FROM snake_duels WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        row = await cur.fetchone()
        return (row["wins"], row["losses"]) if row else (0, 0)
