import aiosqlite
import json
import os
import datetime
import asyncio
from contextlib import suppress

DB_PATH = "data/bot_database.db"
_DB_CONN: aiosqlite.Connection | None = None
_DB_CONN_LOCK = asyncio.Lock()
_LOG_QUEUE: asyncio.Queue[tuple[str, str]] | None = None
_LOG_WORKER_TASK: asyncio.Task | None = None
_LOG_WORKER_STOP = asyncio.Event()

DEFAULT_COVER_IMAGE_URL = "https://cdn.discordapp.com/embed/avatars/0.png"


def is_main_chapter(chapter_num: float | int | str | None) -> bool:
    """
    Determines whether chapter_num is a main chapter (integer value, e.g. 10.0, 12, 100)
    rather than a fractional sub-chapter (e.g. 10.5, 12.1, 10.2).
    """
    if chapter_num is None:
        return False
    try:
        num = float(chapter_num)
        return num.is_integer()
    except (ValueError, TypeError):
        return False


def should_ignore_chapter(chapter_num: float | int | str | None, ignore_sub_chapters: bool = True) -> bool:
    """
    Returns True if ignore_sub_chapters setting is enabled (True) and chapter_num
    is a fractional sub-chapter (not a main chapter).
    """
    if not ignore_sub_chapters:
        return False
    return not is_main_chapter(chapter_num)


def get_valid_cover_url(url: str | None) -> str:
    """
    Returns a valid cover image URL or DEFAULT_COVER_IMAGE_URL if url is empty, broken, or non-HTTP(S).
    """
    if not url or not isinstance(url, str):
        return DEFAULT_COVER_IMAGE_URL
    cleaned = url.strip()
    if cleaned.lower().startswith(("http://", "https://")):
        return cleaned
    return DEFAULT_COVER_IMAGE_URL


async def _get_db() -> aiosqlite.Connection:
    global _DB_CONN
    if _DB_CONN is not None:
        return _DB_CONN
    async with _DB_CONN_LOCK:
        if _DB_CONN is None:
            conn = await aiosqlite.connect(DB_PATH, timeout=30.0)
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            _DB_CONN = conn
    return _DB_CONN


class AsyncWriteQueue:
    """
    Async Write Queue system to enqueue DB write queries serially,
    preventing concurrent write collisions and eliminating `database is locked` errors.
    """
    def __init__(self):
        self._queue: asyncio.Queue | None = None
        self._worker_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()

    async def start(self):
        if self._worker_task and not self._worker_task.done():
            return
        self._queue = asyncio.Queue()
        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self._worker_loop(), name="db-async-write-worker")

    async def stop(self):
        if self._worker_task is None:
            return
        self._stop_event.set()
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker_task
        self._worker_task = None
        self._queue = None

    async def execute(self, query_or_func, params=(), commit=True):
        """
        Enqueues a DB write operation (query string or async callable) to be executed serially.
        """
        if self._worker_task is None or self._worker_task.done():
            await self.start()

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        await self._queue.put((query_or_func, params, commit, future))
        return await future

    async def _run_op(self, query_or_func, params, commit):
        db = await _get_db()
        if callable(query_or_func):
            res = await query_or_func(db)
            if commit:
                await db.commit()
            return res
        else:
            cursor = await db.execute(query_or_func, params)
            if commit:
                await db.commit()
            return cursor

    async def _worker_loop(self):
        while not self._stop_event.is_set():
            try:
                if self._queue is None:
                    await asyncio.sleep(0.05)
                    continue
                item = await self._queue.get()
                query_or_func, params, commit, future = item
                if future.cancelled():
                    self._queue.task_done()
                    continue
                try:
                    res = await self._run_op(query_or_func, params, commit)
                    if not future.done():
                        future.set_result(res)
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc)
                finally:
                    self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[DB Error] AsyncWriteQueue worker failure: {e}")
                await asyncio.sleep(0.05)


_WRITE_QUEUE = AsyncWriteQueue()


async def execute_write_async(query_or_func, params=(), commit=True):
    """
    Enqueues a DB write query or async write callback into the AsyncWriteQueue
    to prevent concurrent SQLite write collisions.
    """
    return await _WRITE_QUEUE.execute(query_or_func, params=params, commit=commit)


async def close_db():
    await stop_log_worker()
    await _WRITE_QUEUE.stop()
    global _DB_CONN
    if _DB_CONN is not None:
        await _DB_CONN.close()
        _DB_CONN = None


async def init_db():
    if not os.path.exists("data"):
        os.makedirs("data")

    await _WRITE_QUEUE.start()
    db = await _get_db()
    await db.execute("""
        CREATE TABLE IF NOT EXISTS trackers (
            tracker_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id         INTEGER,
            channel_id       INTEGER,
            url              TEXT,
            last_chapter     REAL,
            custom_msg       TEXT,
            interval_hours   INTEGER,
            last_checked     TEXT,
            download_enabled INTEGER DEFAULT 0,
            title            TEXT DEFAULT '',
            paused           INTEGER DEFAULT 0,
            mention_str      TEXT DEFAULT ''
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_trackers_guild ON trackers(guild_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_trackers_channel ON trackers(channel_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_trackers_url ON trackers(url)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_permissions (
            user_id  INTEGER PRIMARY KEY,
            rank     INTEGER DEFAULT 1,
            note     TEXT    DEFAULT '',
            vip_expiry TEXT,
            added_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_user_permissions_rank ON user_permissions(rank)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS custom_sites (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            domain     TEXT UNIQUE,
            site_type  TEXT DEFAULT 'madara',
            added_by   INTEGER,
            added_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            notes      TEXT DEFAULT ''
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_custom_sites_type ON custom_sites(site_type)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS stitch_jobs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            title      TEXT,
            status     TEXT DEFAULT 'pending',
            result_url TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_stitch_jobs_user ON stitch_jobs(user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_stitch_jobs_status ON stitch_jobs(status)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_logs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            level     TEXT DEFAULT 'INFO',
            message   TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_bot_logs_timestamp ON bot_logs(timestamp)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_bot_logs_level ON bot_logs(level)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS site_auth (
            domain     TEXT PRIMARY KEY,
            auth_data  TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS credit_transactions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user  INTEGER,
            to_user    INTEGER,
            credit_type TEXT,
            amount     INTEGER,
            reason     TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_credit_trans_from ON credit_transactions(from_user)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_credit_trans_to ON credit_transactions(to_user)")

    # ── Custom Selectors (مثل mantium: css/xpath + regex + headless) ─────
    await db.execute("""
        CREATE TABLE IF NOT EXISTS custom_selectors (
            domain        TEXT PRIMARY KEY,
            selector      TEXT,
            url_attr      TEXT DEFAULT 'href',
            number_regex  TEXT DEFAULT '',
            get_first     INTEGER DEFAULT 0,
            use_browser   INTEGER DEFAULT 0,
            notes         TEXT DEFAULT '',
            raw_config    TEXT DEFAULT '',
            updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_custom_selectors_domain ON custom_selectors(domain)")

    # ── Radar v2: mapping + reminders ─────────────────────────────────────
    await db.execute("""
        CREATE TABLE IF NOT EXISTS tracker_cards (
            message_id   INTEGER PRIMARY KEY,
            tracker_id   INTEGER,
            guild_id     INTEGER,
            channel_id   INTEGER,
            url          TEXT,
            chapter_num  REAL,
            chapter_url  TEXT,
            locked       INTEGER DEFAULT 0,
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            batch_data   TEXT
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tracker_cards_tracker ON tracker_cards(tracker_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tracker_cards_channel ON tracker_cards(channel_id)")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS radar_reminders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id  INTEGER,
            tracker_id  INTEGER,
            guild_id    INTEGER,
            channel_id  INTEGER,
            user_id     INTEGER,
            notify_at   TEXT,
            fired       INTEGER DEFAULT 0,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_radar_reminders_due ON radar_reminders(fired, notify_at)")

    # ── Personal Tracker (per-user, guild-independent) ────────────────────
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_trackers (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id               INTEGER NOT NULL,
            url                   TEXT NOT NULL,
            title                 TEXT DEFAULT '',
            last_chapter          REAL DEFAULT 0,
            last_checked          TEXT,
            interval_minutes      INTEGER DEFAULT 30,
            auto_download         INTEGER DEFAULT 0,
            paused                INTEGER DEFAULT 0,
            notification_channel_id TEXT,
            mention_on_update     INTEGER DEFAULT 0,
            created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
            priority              TEXT DEFAULT 'normal',
            heat_score            REAL DEFAULT 50.0,
            last_release_at       TEXT,
            release_pattern       TEXT,
            check_method          TEXT DEFAULT 'scrape',
            consecutive_failures  INTEGER DEFAULT 0,
            cover_url             TEXT,
            notify_user_id        TEXT,
            notify_role_id        TEXT,
            custom_message        TEXT
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_user_trackers_user ON user_trackers(user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_user_trackers_url ON user_trackers(user_id, url)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_user_trackers_url_only ON user_trackers(url)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_user_trackers_paused_checked ON user_trackers(paused, last_checked)")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_panel_messages (
            user_id    INTEGER PRIMARY KEY,
            channel_id TEXT NOT NULL,
            message_id TEXT NOT NULL
        )
    """)

    # ── Server Tracker V3 (unified per-guild, not per-user) ───────────────
    await db.execute("""
        CREATE TABLE IF NOT EXISTS server_trackers (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id                INTEGER NOT NULL,
            url                     TEXT    NOT NULL,
            title                   TEXT    DEFAULT '',
            cover_url               TEXT,
            notification_channel_id TEXT    NOT NULL,
            mention_role_id         TEXT,
            added_by_user_id        INTEGER,
            last_chapter            REAL    DEFAULT 0,
            last_checked            TEXT,
            paused                  INTEGER DEFAULT 0,
            consecutive_failures    INTEGER DEFAULT 0,
            auto_download           INTEGER DEFAULT 1,
            drive_folder_id         TEXT,
            drive_folder_url        TEXT,
            check_method            TEXT    DEFAULT 'auto',
            heat_score              REAL    DEFAULT 50.0,
            release_pattern         TEXT,
            last_release_at         TEXT,
            priority                TEXT    DEFAULT 'normal',
            created_at              DATETIME DEFAULT CURRENT_TIMESTAMP,
            admin_roles             TEXT    DEFAULT '[]',
            ping_on_update          INTEGER DEFAULT 1,
            ignore_sub_chapters     INTEGER DEFAULT 0,
            UNIQUE(guild_id, url)
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_sv3_guild ON server_trackers(guild_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_sv3_paused ON server_trackers(paused, last_checked)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_sv3_url ON server_trackers(url)")

    # Ensure ignore_sub_chapters column exists for migration compatibility
    for table_name in ("trackers", "server_trackers", "user_trackers"):
        try:
            async with db.execute(f"PRAGMA table_info({table_name})") as c:
                cols = [row[1] for row in await c.fetchall()]
                if cols and "ignore_sub_chapters" not in cols:
                    await db.execute(f"ALTER TABLE {table_name} ADD COLUMN ignore_sub_chapters INTEGER DEFAULT 0")
        except Exception:
            pass

    await db.execute("""
        CREATE TABLE IF NOT EXISTS tracker_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tracker_id    INTEGER NOT NULL,
            chapter_num   REAL    NOT NULL,
            chapter_url   TEXT    DEFAULT '',
            notified      INTEGER DEFAULT 0,
            downloaded    INTEGER DEFAULT 0,
            dl_status     TEXT    DEFAULT '',
            dl_result     TEXT,
            job_id        TEXT,
            drive_file_id TEXT,
            detected_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            notified_at   DATETIME,
            completed_at  DATETIME,
            alert_message_id TEXT,
            UNIQUE(tracker_id, chapter_num),
            FOREIGN KEY(tracker_id) REFERENCES server_trackers(id) ON DELETE CASCADE
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tevt_tracker ON tracker_events(tracker_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tevt_pending ON tracker_events(downloaded, dl_status)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tevt_notified ON tracker_events(notified)")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS tracker_drive_folders (
            tracker_id    INTEGER PRIMARY KEY,
            folder_name   TEXT    NOT NULL,
            folder_id     TEXT,
            folder_url    TEXT,
            chapter_count INTEGER DEFAULT 0,
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(tracker_id) REFERENCES server_trackers(id) ON DELETE CASCADE
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS extract_chunks_cache (
            folder_id  TEXT,
            lang       TEXT,
            chunk_idx  INTEGER,
            data       TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (folder_id, lang, chunk_idx)
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS tracker_subscriptions (
            tracker_id INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tracker_id, user_id)
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tracker_subs_tracker ON tracker_subscriptions(tracker_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tracker_subs_user ON tracker_subscriptions(user_id)")


    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_extraction_settings (
            user_id           INTEGER NOT NULL,
            profile_name      TEXT NOT NULL,
            add_spaces        INTEGER DEFAULT 1,
            remove_sfx        INTEGER DEFAULT 1,
            remove_legends    INTEGER DEFAULT 0,
            connected_slashes INTEGER DEFAULT 0,
            output_format     TEXT DEFAULT 'BOTH',
            model_mode        TEXT DEFAULT 'ADVANCED',
            legend_speech     TEXT DEFAULT '""',
            legend_shouting   TEXT DEFAULT '::',
            legend_small      TEXT DEFAULT 'ST',
            legend_thinking   TEXT DEFAULT '()',
            legend_box        TEXT DEFAULT '[]',
            legend_system     TEXT DEFAULT '<>',
            legend_outer      TEXT DEFAULT 'OT',
            legend_sfx        TEXT DEFAULT 'SFX',
            is_active         INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, profile_name)
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_daily_usage (
            user_id       INTEGER,
            usage_date    TEXT,
            clean_count   INTEGER DEFAULT 0,
            extract_count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, usage_date)
        )
    """)


    # interval_minutes: supports 30m-style checks (migrated from interval_hours)
    try:
        await db.execute("ALTER TABLE trackers ADD COLUMN interval_minutes INTEGER")
    except Exception:
        pass
    await db.execute(
        "UPDATE trackers SET interval_minutes = COALESCE(interval_minutes, interval_hours * 60, 60) "
        "WHERE interval_minutes IS NULL OR interval_minutes < 1"
    )
    await db.commit()

    migrations = [
        "ALTER TABLE trackers ADD COLUMN download_enabled INTEGER DEFAULT 0",
        "ALTER TABLE trackers ADD COLUMN title TEXT DEFAULT ''",
        "ALTER TABLE trackers ADD COLUMN paused INTEGER DEFAULT 0",
        "ALTER TABLE trackers ADD COLUMN mention_str TEXT DEFAULT ''",
        "ALTER TABLE tracker_cards ADD COLUMN batch_data TEXT",
        "ALTER TABLE custom_selectors ADD COLUMN raw_config TEXT DEFAULT ''",
        "ALTER TABLE trackers ADD COLUMN priority TEXT DEFAULT 'normal'",
        "ALTER TABLE trackers ADD COLUMN heat_score REAL DEFAULT 50.0",
        "ALTER TABLE trackers ADD COLUMN last_release_at TEXT",
        "ALTER TABLE trackers ADD COLUMN release_pattern TEXT",
        "ALTER TABLE trackers ADD COLUMN check_method TEXT DEFAULT 'scrape'",
        "ALTER TABLE trackers ADD COLUMN consecutive_failures INTEGER DEFAULT 0",
        "ALTER TABLE user_trackers ADD COLUMN priority TEXT DEFAULT 'normal'",
        "ALTER TABLE user_trackers ADD COLUMN heat_score REAL DEFAULT 50.0",
        "ALTER TABLE user_trackers ADD COLUMN last_release_at TEXT",
        "ALTER TABLE user_trackers ADD COLUMN release_pattern TEXT",
        "ALTER TABLE user_trackers ADD COLUMN check_method TEXT DEFAULT 'scrape'",
        "ALTER TABLE user_trackers ADD COLUMN consecutive_failures INTEGER DEFAULT 0",
        "ALTER TABLE user_trackers ADD COLUMN cover_url TEXT",
        "ALTER TABLE user_trackers ADD COLUMN notify_user_id TEXT",
        "ALTER TABLE user_trackers ADD COLUMN notify_role_id TEXT",
        "ALTER TABLE user_trackers ADD COLUMN custom_message TEXT",
        "ALTER TABLE user_permissions ADD COLUMN inpainting_credits INTEGER DEFAULT 0",
        "ALTER TABLE user_permissions ADD COLUMN extraction_credits INTEGER DEFAULT 0",
        "ALTER TABLE user_permissions ADD COLUMN used_trial_clean INTEGER DEFAULT 0",
        "ALTER TABLE user_permissions ADD COLUMN used_trial_extract INTEGER DEFAULT 0",
        "ALTER TABLE user_permissions ADD COLUMN vip_expiry TEXT",
        "ALTER TABLE server_trackers ADD COLUMN admin_roles TEXT DEFAULT '[]'",
        "ALTER TABLE server_trackers ADD COLUMN ping_on_update INTEGER DEFAULT 1",
        "ALTER TABLE tracker_events ADD COLUMN alert_message_id TEXT",
    ]
    for sql in migrations:
        try:
            await db.execute(sql)
            await db.commit()
        except Exception:
            pass

    # ── Self-Migration: Set 30-day VIP expiry for any existing VIP users without an expiry date ──
    try:
        default_expiry_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
        default_expiry_iso = default_expiry_dt.isoformat()
        await db.execute(
            "UPDATE user_permissions SET vip_expiry = ? WHERE rank = 2 AND (vip_expiry IS NULL OR vip_expiry = '')",
            (default_expiry_iso,)
        )
        await db.commit()
    except Exception:
        pass

    await start_log_worker()


async def start_log_worker():
    global _LOG_QUEUE, _LOG_WORKER_TASK
    if _LOG_WORKER_TASK and not _LOG_WORKER_TASK.done():
        return
    _LOG_QUEUE = asyncio.Queue(maxsize=5000)
    _LOG_WORKER_STOP.clear()
    _LOG_WORKER_TASK = asyncio.create_task(_log_worker_loop(), name="db-log-worker")


async def stop_log_worker():
    global _LOG_WORKER_TASK, _LOG_QUEUE
    if _LOG_WORKER_TASK is None:
        return
    _LOG_WORKER_STOP.set()
    try:
        await _LOG_WORKER_TASK
    except BaseException:
        pass
    _LOG_WORKER_TASK = None
    _LOG_QUEUE = None


async def _write_logs_batch(batch: list[tuple[str, str]]):
    if not batch:
        return
    db = await _get_db()
    await db.executemany("INSERT INTO bot_logs (level, message) VALUES (?, ?)", batch)
    # Keep only last 500 logs
    await db.execute("DELETE FROM bot_logs WHERE id NOT IN (SELECT id FROM bot_logs ORDER BY id DESC LIMIT 500)")
    await db.commit()


async def _log_worker_loop():
    batch: list[tuple[str, str]] = []
    while not _LOG_WORKER_STOP.is_set():
        try:
            if _LOG_QUEUE is None:
                await asyncio.sleep(0.2)
                continue
            item = await asyncio.wait_for(_LOG_QUEUE.get(), timeout=0.5)
            batch.append(item)
            # small burst drain
            for _ in range(49):
                if _LOG_QUEUE.empty():
                    break
                batch.append(_LOG_QUEUE.get_nowait())
            await _write_logs_batch(batch)
            batch.clear()
        except asyncio.TimeoutError:
            if batch:
                await _write_logs_batch(batch)
                batch.clear()
        except Exception as e:
            print(f"[DB Error] Log worker failure: {e}")
            await asyncio.sleep(0.2)

    # final drain
    if _LOG_QUEUE is not None:
        try:
            while not _LOG_QUEUE.empty():
                batch.append(_LOG_QUEUE.get_nowait())
        except Exception:
            pass
    if batch:
        try:
            await _write_logs_batch(batch)
        except BaseException:
            pass


async def log_event(level: str, message: str):
    try:
        payload = (level, message[:1000])
        if _LOG_QUEUE is not None and _LOG_WORKER_TASK is not None and not _LOG_WORKER_TASK.done():
            try:
                _LOG_QUEUE.put_nowait(payload)
                return
            except asyncio.QueueFull:
                # fallback direct write when queue is full
                pass
        await _write_logs_batch([payload])
    except Exception as e:
        print(f"[DB Error] Failed to log event: {e}")


async def get_recent_logs(limit: int = 100):
    db = await _get_db()
    async with db.execute("SELECT level, message, timestamp FROM bot_logs ORDER BY id DESC LIMIT ?", (limit,)) as cursor:
        return await cursor.fetchall()


async def add_tracker(
    guild_id,
    channel_id,
    url,
    custom_msg,
    interval_hours,
    current_chapter,
    download_enabled=0,
    title="",
    mention_str="",
    interval_minutes: int | None = None,
):
    db = await _get_db()
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if interval_minutes is None:
        interval_minutes = max(1, int(interval_hours or 1) * 60)
    ih = max(1, (int(interval_minutes) + 59) // 60)
    await db.execute(
        "INSERT INTO trackers (guild_id, channel_id, url, last_chapter, custom_msg, interval_hours, "
        "last_checked, download_enabled, title, mention_str, interval_minutes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            guild_id,
            channel_id,
            url,
            current_chapter,
            custom_msg,
            ih,
            now_str,
            download_enabled,
            title,
            mention_str,
            int(interval_minutes),
        ),
    )
    await db.commit()


async def get_all_trackers():
    db = await _get_db()
    async with db.execute(
        "SELECT tracker_id, guild_id, channel_id, url, last_chapter, custom_msg, interval_hours, "
        "last_checked, download_enabled, paused, title, mention_str, interval_minutes, "
        "priority, heat_score, last_release_at, release_pattern, check_method, consecutive_failures FROM trackers"
    ) as cursor:
        return await cursor.fetchall()


async def get_tracker(tracker_id: int, guild_id: int) -> tuple | None:
    db = await _get_db()
    async with db.execute(
        "SELECT tracker_id, guild_id, channel_id, url, last_chapter, custom_msg, interval_hours, "
        "last_checked, download_enabled, paused, title, mention_str, interval_minutes, "
        "priority, heat_score, last_release_at, release_pattern, check_method, consecutive_failures "
        "FROM trackers WHERE tracker_id=? AND guild_id=?",
        (tracker_id, guild_id),
    ) as cursor:
        return await cursor.fetchone()


async def set_tracker_paused(tracker_id: int, guild_id: int, paused: int) -> bool:
    db = await _get_db()
    await db.execute("UPDATE trackers SET paused=? WHERE tracker_id=? AND guild_id=?", (1 if paused else 0, tracker_id, guild_id))
    await db.commit()
    return True


async def upsert_tracker_card(
    message_id: int,
    tracker_id: int,
    guild_id: int,
    channel_id: int,
    url: str,
    chapter_num: float,
    chapter_url: str,
    locked: int = 0,
    batch_data: str | None = None,
) -> None:
    db = await _get_db()
    await db.execute(
        "INSERT INTO tracker_cards (message_id, tracker_id, guild_id, channel_id, url, chapter_num, chapter_url, locked, batch_data) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(message_id) DO UPDATE SET "
        "tracker_id=excluded.tracker_id, guild_id=excluded.guild_id, channel_id=excluded.channel_id, url=excluded.url, "
        "chapter_num=excluded.chapter_num, chapter_url=excluded.chapter_url, locked=excluded.locked, batch_data=excluded.batch_data",
        (int(message_id), int(tracker_id), int(guild_id), int(channel_id), str(url), float(chapter_num), str(chapter_url), int(locked), batch_data),
    )
    await db.commit()


async def get_tracker_card(message_id: int) -> dict | None:
    db = await _get_db()
    async with db.execute(
        "SELECT message_id, tracker_id, guild_id, channel_id, url, chapter_num, chapter_url, locked, created_at, batch_data "
        "FROM tracker_cards WHERE message_id=?",
        (int(message_id),),
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return None
    mid, tid, gid, cid, url, ch_num, ch_url, locked, created_at, batch_data = row
    return {
        "message_id": mid,
        "tracker_id": tid,
        "guild_id": gid,
        "channel_id": cid,
        "url": url,
        "chapter_num": ch_num,
        "chapter_url": ch_url,
        "locked": bool(locked),
        "created_at": created_at,
        "batch_data": batch_data,
    }


async def add_radar_reminder(
    message_id: int,
    tracker_id: int,
    guild_id: int,
    channel_id: int,
    user_id: int,
    notify_at_iso: str,
) -> int:
    db = await _get_db()
    async with db.execute(
        "INSERT INTO radar_reminders (message_id, tracker_id, guild_id, channel_id, user_id, notify_at, fired) "
        "VALUES (?, ?, ?, ?, ?, ?, 0)",
        (int(message_id), int(tracker_id), int(guild_id), int(channel_id), int(user_id), str(notify_at_iso)),
    ) as cursor:
        await db.commit()
        return cursor.lastrowid


async def get_due_radar_reminders(now_iso: str, limit: int = 50) -> list[tuple]:
    db = await _get_db()
    async with db.execute(
        "SELECT id, message_id, tracker_id, guild_id, channel_id, user_id, notify_at "
        "FROM radar_reminders WHERE fired=0 AND notify_at <= ? ORDER BY notify_at ASC LIMIT ?",
        (str(now_iso), int(limit)),
    ) as cursor:
        return await cursor.fetchall()


async def mark_radar_reminder_fired(reminder_id: int) -> None:
    db = await _get_db()
    await db.execute("UPDATE radar_reminders SET fired=1 WHERE id=?", (int(reminder_id),))
    await db.commit()


async def remove_tracker(tracker_id, guild_id):
    db = await _get_db()
    async with db.execute("SELECT 1 FROM trackers WHERE tracker_id = ? AND guild_id = ?", (tracker_id, guild_id)) as cursor:
        if await cursor.fetchone() is None:
            return False
    await db.execute("DELETE FROM trackers WHERE tracker_id = ? AND guild_id = ?", (tracker_id, guild_id))
    await db.commit()
    return True


async def update_tracker_time(tracker_id, last_checked_str):
    db = await _get_db()
    await db.execute("UPDATE trackers SET last_checked = ? WHERE tracker_id = ?", (last_checked_str, tracker_id))
    await db.commit()


async def update_tracker_chapter(tracker_id, new_chapter, last_checked_str):
    db = await _get_db()
    await db.execute("UPDATE trackers SET last_chapter = ?, last_checked = ? WHERE tracker_id = ?", (new_chapter, last_checked_str, tracker_id))
    await db.commit()


async def get_tracker_count():
    db = await _get_db()
    async with db.execute("SELECT COUNT(*) FROM trackers") as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0


async def add_custom_site(domain: str, site_type: str, added_by: int, notes: str = ""):
    db = await _get_db()
    await db.execute(
        "INSERT INTO custom_sites (domain, site_type, added_by, notes) VALUES (?, ?, ?, ?) ON CONFLICT(domain) DO UPDATE SET site_type=excluded.site_type, notes=excluded.notes",
        (domain.lower().strip(), site_type, added_by, notes),
    )
    await db.commit()


async def get_custom_sites():
    db = await _get_db()
    async with db.execute("SELECT domain, site_type, added_by, added_at, notes FROM custom_sites ORDER BY added_at DESC") as cursor:
        return await cursor.fetchall()


async def set_custom_selector_rule(
    domain: str,
    selector: str,
    url_attr: str = "href",
    number_regex: str = "",
    get_first: int = 0,
    use_browser: int = 0,
    notes: str = "",
    raw_config: str = "",
):
    db = await _get_db()
    domain = domain.lower().strip()
    await db.execute(
        "INSERT INTO custom_selectors (domain, selector, url_attr, number_regex, get_first, use_browser, notes, raw_config, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(domain) DO UPDATE SET "
        "selector=excluded.selector, url_attr=excluded.url_attr, number_regex=excluded.number_regex, "
        "get_first=excluded.get_first, use_browser=excluded.use_browser, notes=excluded.notes, raw_config=excluded.raw_config, updated_at=CURRENT_TIMESTAMP",
        (domain, selector.strip(), (url_attr or "href").strip(), (number_regex or "").strip(), int(get_first or 0), int(use_browser or 0), (notes or "").strip(), (raw_config or "").strip()),
    )
    await db.commit()


async def remove_custom_selector_rule(domain: str):
    db = await _get_db()
    await db.execute("DELETE FROM custom_selectors WHERE domain=?", (domain.lower().strip(),))
    await db.commit()


async def get_custom_selector_rules() -> list[tuple]:
    db = await _get_db()
    async with db.execute(
        "SELECT domain, selector, url_attr, number_regex, get_first, use_browser, notes, raw_config, updated_at "
        "FROM custom_selectors ORDER BY updated_at DESC"
    ) as cursor:
        return await cursor.fetchall()


async def get_custom_selector_rule(domain: str) -> dict | None:
    db = await _get_db()
    async with db.execute(
        "SELECT domain, selector, url_attr, number_regex, get_first, use_browser, notes, raw_config, updated_at "
        "FROM custom_selectors WHERE domain=?",
        (domain.lower().strip(),),
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return None
    d, sel, url_attr, num_re, get_first, use_browser, notes, raw_config, updated_at = row
    return {
        "domain": d,
        "selector": sel,
        "url_attr": url_attr or "href",
        "number_regex": num_re or "",
        "get_first": bool(get_first),
        "use_browser": bool(use_browser),
        "notes": notes or "",
        "raw_config": raw_config or "",
        "updated_at": updated_at,
    }


async def remove_custom_site(domain: str):
    db = await _get_db()
    await db.execute("DELETE FROM custom_sites WHERE domain = ?", (domain.lower().strip(),))
    await db.commit()


async def get_custom_madara_sites() -> list:
    db = await _get_db()
    async with db.execute("SELECT domain FROM custom_sites WHERE site_type = 'madara'") as cursor:
        return [row[0] for row in await cursor.fetchall()]


async def get_custom_arabic_sites() -> list:
    db = await _get_db()
    async with db.execute("SELECT domain FROM custom_sites WHERE site_type = 'arabic'") as cursor:
        return [row[0] for row in await cursor.fetchall()]


async def create_stitch_job(user_id: int, title: str) -> int:
    db = await _get_db()
    async with db.execute("INSERT INTO stitch_jobs (user_id, title, status) VALUES (?, ?, 'pending')", (user_id, title)) as cursor:
        await db.commit()
        return cursor.lastrowid


async def update_stitch_job(job_id: int, status: str, result_url: str = ""):
    db = await _get_db()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    await db.execute("UPDATE stitch_jobs SET status=?, result_url=?, updated_at=? WHERE id=?", (status, result_url, now, job_id))
    await db.commit()


async def get_user_rank(user_id: int, auto_register: bool = False) -> int:
    db = await _get_db()
    async with db.execute("SELECT rank, vip_expiry FROM user_permissions WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        if row:
            rank, vip_expiry = row
            if rank == 2 and vip_expiry:
                try:
                    expiry_dt = datetime.datetime.fromisoformat(vip_expiry)
                    if datetime.datetime.now(datetime.timezone.utc) > expiry_dt:
                        await db.execute("UPDATE user_permissions SET rank = 1, vip_expiry = NULL, note = 'VIP Expired' WHERE user_id = ?", (user_id,))
                        await db.commit()
                        return 1
                except Exception:
                    pass
            return rank
        if auto_register:
            await db.execute("INSERT OR IGNORE INTO user_permissions (user_id, rank, note) VALUES (?, ?, ?)", (user_id, 1, "Auto-registered"))
            await db.commit()
            return 1
        return 0


async def set_user_rank(user_id: int, rank: int, note: str = ""):
    db = await _get_db()
    if rank == 2:
        default_expiry = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)).isoformat()
        await db.execute(
            "INSERT INTO user_permissions (user_id, rank, note, vip_expiry) VALUES (?, 2, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET rank=2, note=excluded.note, "
            "vip_expiry=COALESCE(user_permissions.vip_expiry, excluded.vip_expiry)",
            (user_id, note, default_expiry),
        )
    else:
        await db.execute(
            "INSERT INTO user_permissions (user_id, rank, note) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET rank=excluded.rank, note=excluded.note",
            (user_id, rank, note),
        )
    await db.commit()


async def set_user_vip_expiry(user_id: int, vip_expiry_iso: str | None) -> None:
    db = await _get_db()
    await db.execute(
        "UPDATE user_permissions SET vip_expiry = ? WHERE user_id = ?",
        (vip_expiry_iso, user_id)
    )
    await db.commit()


async def remove_user(user_id: int):
    db = await _get_db()
    await db.execute("DELETE FROM user_permissions WHERE user_id = ?", (user_id,))
    await db.commit()


async def get_all_users() -> list:
    db = await _get_db()
    async with db.execute("SELECT user_id, rank, note, added_at FROM user_permissions ORDER BY rank DESC") as cursor:
        return await cursor.fetchall()


async def get_user_count() -> int:
    db = await _get_db()
    async with db.execute("SELECT COUNT(*) FROM user_permissions") as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0


async def set_site_auth(domain: str, auth_data: dict):
    db = await _get_db()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO site_auth (domain, auth_data, updated_at) VALUES (?, ?, ?) ON CONFLICT(domain) DO UPDATE SET auth_data=excluded.auth_data, updated_at=excluded.updated_at",
        (domain.lower().strip(), json.dumps(auth_data), now),
    )
    await db.commit()


async def get_site_auth(domain: str) -> dict:
    db = await _get_db()
    async with db.execute("SELECT auth_data FROM site_auth WHERE domain = ?", (domain.lower().strip(),)) as cursor:
        row = await cursor.fetchone()
        if row:
            try:
                return json.loads(row[0])
            except Exception:
                pass
        return {}


async def remove_site_auth(domain: str):
    db = await _get_db()
    await db.execute("DELETE FROM site_auth WHERE domain = ?", (domain.lower().strip(),))
    await db.commit()


async def get_all_site_auth() -> list:
    db = await _get_db()
    async with db.execute("SELECT domain, updated_at FROM site_auth") as cursor:
        return await cursor.fetchall()


async def get_all_site_auth_data() -> dict:
    out: dict[str, dict] = {}
    db = await _get_db()
    async with db.execute("SELECT domain, auth_data FROM site_auth") as cursor:
        rows = await cursor.fetchall()
    for domain, auth_json in rows:
        try:
            out[domain] = json.loads(auth_json) if auth_json else {}
        except Exception:
            out[domain] = {}
    return out


async def set_setting(key: str, value: str):
    db = await _get_db()
    await db.execute(
        "INSERT INTO bot_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    await db.commit()


async def get_setting(key: str, default: str = "") -> str:
    db = await _get_db()
    async with db.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else default


# ── Personal Tracker CRUD ──────────────────────────────────────────────

USER_TRACKER_COLUMNS = (
    "id, user_id, url, title, last_chapter, last_checked, interval_minutes, "
    "auto_download, paused, notification_channel_id, mention_on_update, created_at, "
    "priority, heat_score, last_release_at, release_pattern, check_method, consecutive_failures, "
    "cover_url, notify_user_id, notify_role_id, custom_message"
)

def _row_to_user_tracker(row) -> dict:
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "url": row[2],
        "title": row[3],
        "last_chapter": row[4],
        "last_checked": row[5],
        "interval_minutes": row[6],
        "auto_download": row[7],
        "paused": row[8],
        "notification_channel_id": row[9],
        "mention_on_update": row[10],
        "created_at": row[11],
        "priority": row[12] if len(row) > 12 else "normal",
        "heat_score": row[13] if len(row) > 13 else 50.0,
        "last_release_at": row[14] if len(row) > 14 else None,
        "release_pattern": row[15] if len(row) > 15 else None,
        "check_method": row[16] if len(row) > 16 else "scrape",
        "consecutive_failures": row[17] if len(row) > 17 else 0,
        "cover_url": row[18] if len(row) > 18 else None,
        "notify_user_id": row[19] if len(row) > 19 else None,
        "notify_role_id": row[20] if len(row) > 20 else None,
        "custom_message": row[21] if len(row) > 21 else None,
    }

async def add_user_tracker(
    user_id: int,
    url: str,
    title: str = "",
    last_chapter: float = 0,
    interval_minutes: int = 30,
    auto_download: int = 0,
    notification_channel_id: str = "",
    mention_on_update: int = 0,
    cover_url: str = None,
    notify_user_id: str = None,
    notify_role_id: str = None,
    custom_message: str = None,
) -> int:
    db = await _get_db()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO user_trackers (user_id, url, title, last_chapter, last_checked, "
        "interval_minutes, auto_download, notification_channel_id, mention_on_update, "
        "cover_url, notify_user_id, notify_role_id, custom_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, url, title, last_chapter, now, interval_minutes, auto_download,
         notification_channel_id, mention_on_update, cover_url, notify_user_id, notify_role_id, custom_message),
    )
    await db.commit()
    return db.lastrowid


async def get_user_trackers(user_id: int) -> list[dict]:
    db = await _get_db()
    async with db.execute(
        f"SELECT {USER_TRACKER_COLUMNS} FROM user_trackers WHERE user_id=? ORDER BY id ASC",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_user_tracker(row) for row in rows]


async def get_user_tracker(tracker_id: int, user_id: int) -> dict | None:
    db = await _get_db()
    async with db.execute(
        f"SELECT {USER_TRACKER_COLUMNS} FROM user_trackers WHERE id=? AND user_id=?",
        (tracker_id, user_id),
    ) as cursor:
        row = await cursor.fetchone()
    return _row_to_user_tracker(row) if row else None


async def update_user_tracker(tracker_id: int, user_id: int, **kwargs) -> bool:
    if not kwargs:
        return False
    # Whitelist check
    allowed_columns = {
        "url", "title", "last_chapter", "last_checked", "interval_minutes",
        "auto_download", "paused", "notification_channel_id", "mention_on_update",
        "priority", "heat_score", "last_release_at", "release_pattern", "check_method",
        "consecutive_failures", "cover_url", "notify_user_id", "notify_role_id", "custom_message"
    }
    filtered_kwargs = {k: v for k, v in kwargs.items() if k in allowed_columns}
    if not filtered_kwargs:
        return False

    db = await _get_db()
    sets = ", ".join(f"{k}=?" for k in filtered_kwargs)
    vals = list(filtered_kwargs.values()) + [tracker_id, user_id]
    await db.execute(
        f"UPDATE user_trackers SET {sets} WHERE id=? AND user_id=?", vals
    )
    await db.commit()
    return True


async def delete_user_tracker(tracker_id: int, user_id: int) -> bool:
    db = await _get_db()
    await db.execute("DELETE FROM user_trackers WHERE id=? AND user_id=?", (tracker_id, user_id))
    await db.commit()
    return True


async def get_user_tracker_count(user_id: int) -> int:
    db = await _get_db()
    async with db.execute("SELECT COUNT(*) FROM user_trackers WHERE user_id=?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_all_active_user_trackers() -> list[dict]:
    """كل المتابعات النشطة (غير الموقوفة) للفحص في الخلفية"""
    db = await _get_db()
    async with db.execute(
        f"SELECT {USER_TRACKER_COLUMNS} FROM user_trackers WHERE paused=0 ORDER BY last_checked ASC"
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_user_tracker(row) for row in rows]


async def set_panel_message(user_id: int, channel_id: int, message_id: int):
    db = await _get_db()
    await db.execute(
        "INSERT INTO user_panel_messages (user_id, channel_id, message_id) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET channel_id=excluded.channel_id, message_id=excluded.message_id",
        (user_id, str(channel_id), str(message_id)),
    )
    await db.commit()


async def get_panel_message(user_id: int) -> dict | None:
    db = await _get_db()
    async with db.execute(
        "SELECT channel_id, message_id FROM user_panel_messages WHERE user_id=?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return None
    return {"channel_id": row[0], "message_id": row[1]}


async def clear_panel_message(user_id: int):
    db = await _get_db()
    await db.execute("DELETE FROM user_panel_messages WHERE user_id=?", (user_id,))
    await db.commit()


async def get_locked_tracker_cards(days_limit: int = 7) -> list[dict]:
    db = await _get_db()
    async with db.execute(
        "SELECT message_id, tracker_id, guild_id, channel_id, url, chapter_num, chapter_url, locked, created_at "
        "FROM tracker_cards WHERE locked = 1 AND created_at >= datetime('now', ?)",
        (f"-{days_limit} days",)
    ) as cursor:
        rows = await cursor.fetchall()
    result = []
    for row in rows:
        result.append({
            "message_id": row[0],
            "tracker_id": row[1],
            "guild_id": row[2],
            "channel_id": row[3],
            "url": row[4],
            "chapter_num": row[5],
            "chapter_url": row[6],
            "locked": bool(row[7]),
            "created_at": row[8],
        })
    return result


async def update_tracker_card_locked(message_id: int, locked: int) -> None:
    db = await _get_db()
    await db.execute(
        "UPDATE tracker_cards SET locked=? WHERE message_id=?",
        (int(locked), int(message_id))
    )
    await db.commit()


async def get_all_user_trackers_count() -> int:
    db = await _get_db()
    async with db.execute("SELECT COUNT(*) FROM user_trackers") as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0


# ── Real-Time Polling DB Helpers ───────────────────────────────────────────

async def update_tracker_polling_state(tracker_id: int, heat_score: float, priority: str, check_method: str, consecutive_failures: int) -> None:
    db = await _get_db()
    await db.execute(
        "UPDATE trackers SET heat_score=?, priority=?, check_method=?, consecutive_failures=? WHERE tracker_id=?",
        (float(heat_score), str(priority), str(check_method), int(consecutive_failures), int(tracker_id))
    )
    await db.commit()


async def update_tracker_release_pattern(tracker_id: int, release_pattern_json: str, last_release_at_iso: str) -> None:
    db = await _get_db()
    await db.execute(
        "UPDATE trackers SET release_pattern=?, last_release_at=? WHERE tracker_id=?",
        (str(release_pattern_json), str(last_release_at_iso), int(tracker_id))
    )
    await db.commit()


async def update_user_tracker_polling_state(tracker_id: int, heat_score: float, priority: str, check_method: str, consecutive_failures: int) -> None:
    db = await _get_db()
    await db.execute(
        "UPDATE user_trackers SET heat_score=?, priority=?, check_method=?, consecutive_failures=? WHERE id=?",
        (float(heat_score), str(priority), str(check_method), int(consecutive_failures), int(tracker_id))
    )
    await db.commit()


async def update_user_tracker_release_pattern(tracker_id: int, release_pattern_json: str, last_release_at_iso: str) -> None:
    db = await _get_db()
    await db.execute(
        "UPDATE user_trackers SET release_pattern=?, last_release_at=? WHERE id=?",
        (str(release_pattern_json), str(last_release_at_iso), int(tracker_id))
    )
    await db.commit()


async def update_tracker(tracker_id: int, guild_id: int, **kwargs) -> bool:
    if not kwargs:
        return False
    db = await _get_db()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [int(tracker_id), int(guild_id)]
    await db.execute(
        f"UPDATE trackers SET {sets} WHERE tracker_id=? AND guild_id=?", vals
    )
    await db.commit()
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# ── Server Tracker V3 CRUD ─────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

_SV3_COLS = (
    "id, guild_id, url, title, cover_url, notification_channel_id, mention_role_id, "
    "added_by_user_id, last_chapter, last_checked, paused, consecutive_failures, "
    "auto_download, drive_folder_id, drive_folder_url, check_method, heat_score, "
    "release_pattern, last_release_at, priority, created_at, admin_roles, ping_on_update, "
    "ignore_sub_chapters"
)

def _sv3_row(row) -> dict | None:
    if not row:
        return None
    keys = [
        "id", "guild_id", "url", "title", "cover_url",
        "notification_channel_id", "mention_role_id", "added_by_user_id",
        "last_chapter", "last_checked", "paused", "consecutive_failures",
        "auto_download", "drive_folder_id", "drive_folder_url",
        "check_method", "heat_score", "release_pattern",
        "last_release_at", "priority", "created_at", "admin_roles", "ping_on_update",
        "ignore_sub_chapters",
    ]
    return dict(zip(keys, row))


async def sv3_add(
    guild_id: int,
    url: str,
    notification_channel_id: str,
    *,
    title: str = "",
    cover_url: str | None = None,
    mention_role_id: str | None = None,
    added_by_user_id: int | None = None,
    last_chapter: float = 0.0,
    auto_download: int = 1,
    check_method: str = "auto",
    ignore_sub_chapters: int = 0,
) -> int | None:
    """إضافة تراكر جديد. يرجع None لو مضاف مسبقاً (UNIQUE constraint)."""
    db = await _get_db()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        cursor = await db.execute(
            "INSERT INTO server_trackers "
            "(guild_id, url, title, cover_url, notification_channel_id, mention_role_id, "
            "added_by_user_id, last_chapter, last_checked, auto_download, check_method, ignore_sub_chapters) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (guild_id, url, title, cover_url, notification_channel_id,
             mention_role_id, added_by_user_id, last_chapter, now,
             auto_download, check_method, ignore_sub_chapters),
        )
        await db.commit()
        return cursor.lastrowid
    except Exception:
        return None  # UNIQUE violation — already exists


async def sv3_get(tracker_id: int, guild_id: int) -> dict | None:
    db = await _get_db()
    async with db.execute(
        f"SELECT {_SV3_COLS} FROM server_trackers WHERE id=? AND guild_id=?",
        (tracker_id, guild_id),
    ) as c:
        return _sv3_row(await c.fetchone())


async def sv3_get_by_url(guild_id: int, url: str) -> dict | None:
    db = await _get_db()
    async with db.execute(
        f"SELECT {_SV3_COLS} FROM server_trackers WHERE guild_id=? AND url=?",
        (guild_id, url),
    ) as c:
        return _sv3_row(await c.fetchone())


async def sv3_list(guild_id: int) -> list[dict]:
    db = await _get_db()
    async with db.execute(
        f"SELECT {_SV3_COLS} FROM server_trackers WHERE guild_id=? ORDER BY id ASC",
        (guild_id,),
    ) as c:
        return [_sv3_row(r) for r in await c.fetchall()]


async def sv3_all_active() -> list[dict]:
    """كل التراكرز النشطة في كل السيرفرات (للـ background engine)."""
    db = await _get_db()
    async with db.execute(
        f"SELECT {_SV3_COLS} FROM server_trackers WHERE paused=0 ORDER BY last_checked ASC"
    ) as c:
        return [_sv3_row(r) for r in await c.fetchall()]


async def sv3_update(tracker_id: int, guild_id: int, **kwargs) -> bool:
    if not kwargs:
        return False
    allowed = {
        "title", "cover_url", "notification_channel_id", "mention_role_id",
        "last_chapter", "last_checked", "paused", "consecutive_failures",
        "auto_download", "drive_folder_id", "drive_folder_url", "check_method",
        "heat_score", "release_pattern", "last_release_at", "priority",
        "admin_roles", "ping_on_update", "ignore_sub_chapters",
    }
    kw = {k: v for k, v in kwargs.items() if k in allowed}
    if not kw:
        return False
    db = await _get_db()
    sets = ", ".join(f"{k}=?" for k in kw)
    await db.execute(
        f"UPDATE server_trackers SET {sets} WHERE id=? AND guild_id=?",
        list(kw.values()) + [tracker_id, guild_id],
    )
    await db.commit()
    return True


async def sv3_delete(tracker_id: int, guild_id: int) -> bool:
    db = await _get_db()
    await db.execute(
        "DELETE FROM server_trackers WHERE id=? AND guild_id=?",
        (tracker_id, guild_id),
    )
    await db.commit()
    return True


async def sv3_count(guild_id: int) -> int:
    db = await _get_db()
    async with db.execute(
        "SELECT COUNT(*) FROM server_trackers WHERE guild_id=?", (guild_id,)
    ) as c:
        row = await c.fetchone()
        return row[0] if row else 0


# ── tracker_events ────────────────────────────────────────────────────────────

async def tevt_try_insert(tracker_id: int, chapter_num: float, chapter_url: str = "") -> bool:
    """
    يحاول إدراج حدث فصل جديد.
    يرجع True لو تم الإدراج (فصل جديد)، False لو موجود مسبقاً.
    هذه هي طبقة الـ anti-duplicate الأولى.
    """
    db = await _get_db()
    try:
        await db.execute(
            "INSERT INTO tracker_events (tracker_id, chapter_num, chapter_url) VALUES (?,?,?)",
            (tracker_id, chapter_num, chapter_url),
        )
        await db.commit()
        return True
    except Exception:
        return False  # UNIQUE constraint → chapter already registered


async def tevt_mark_notified(event_id: int, alert_message_id: str = None) -> None:
    db = await _get_db()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    await db.execute(
        "UPDATE tracker_events SET notified=1, notified_at=?, alert_message_id=? WHERE id=?",
        (now, alert_message_id, event_id),
    )
    await db.commit()


async def tevt_set_dl_pending(event_id: int, job_id: str = "") -> None:
    db = await _get_db()
    await db.execute(
        "UPDATE tracker_events SET dl_status='pending', job_id=? WHERE id=?",
        (job_id, event_id),
    )
    await db.commit()


async def tevt_set_dl_completed(event_id: int, result_url: str, drive_file_id: str = "") -> None:
    db = await _get_db()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    await db.execute(
        "UPDATE tracker_events SET downloaded=1, dl_status='completed', "
        "dl_result=?, drive_file_id=?, completed_at=? WHERE id=?",
        (result_url, drive_file_id, now, event_id),
    )
    await db.commit()


async def tevt_set_dl_failed(event_id: int, reason: str = "") -> None:
    db = await _get_db()
    await db.execute(
        "UPDATE tracker_events SET dl_status='failed', dl_result=? WHERE id=?",
        (reason, event_id),
    )
    await db.commit()


async def tevt_get(tracker_id: int, chapter_num: float) -> dict | None:
    db = await _get_db()
    async with db.execute(
        "SELECT id, tracker_id, chapter_num, chapter_url, notified, downloaded, "
        "dl_status, dl_result, job_id, drive_file_id, detected_at, alert_message_id "
        "FROM tracker_events WHERE tracker_id=? AND chapter_num=?",
        (tracker_id, chapter_num),
    ) as c:
        row = await c.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "tracker_id": row[1], "chapter_num": row[2],
        "chapter_url": row[3], "notified": row[4], "downloaded": row[5],
        "dl_status": row[6], "dl_result": row[7], "job_id": row[8],
        "drive_file_id": row[9], "detected_at": row[10], "alert_message_id": row[11],
    }


async def tevt_get_pending_downloads() -> list[dict]:
    """كل الأحداث التي لم يكتمل تحميلها — للـ crash recovery."""
    db = await _get_db()
    async with db.execute(
        "SELECT e.id, e.tracker_id, e.chapter_num, e.chapter_url, e.job_id, "
        "t.guild_id, t.title, t.notification_channel_id, t.added_by_user_id, "
        "t.cover_url, t.url AS series_url, e.alert_message_id "
        "FROM tracker_events e "
        "JOIN server_trackers t ON t.id = e.tracker_id "
        "WHERE e.dl_status='pending' AND t.paused=0"
    ) as c:
        rows = await c.fetchall()
    return [
        {
            "event_id": r[0], "tracker_id": r[1], "chapter_num": r[2],
            "chapter_url": r[3], "job_id": r[4], "guild_id": r[5],
            "title": r[6], "notification_channel_id": r[7],
            "added_by_user_id": r[8], "cover_url": r[9], "series_url": r[10],
            "alert_message_id": r[11],
        }
        for r in rows
    ]


async def tevt_cleanup_old(days: int = 60) -> None:
    """حذف الأحداث القديمة المكتملة (أقدم من N يوم) للحفاظ على حجم DB."""
    db = await _get_db()
    await db.execute(
        "DELETE FROM tracker_events WHERE dl_status='completed' "
        "AND detected_at < datetime('now', ?)",
        (f"-{days} days",),
    )
    await db.commit()


# ── tracker_drive_folders ─────────────────────────────────────────────────────

async def tdf_get(tracker_id: int) -> dict | None:
    db = await _get_db()
    async with db.execute(
        "SELECT tracker_id, folder_name, folder_id, folder_url, chapter_count "
        "FROM tracker_drive_folders WHERE tracker_id=?",
        (tracker_id,),
    ) as c:
        row = await c.fetchone()
    if not row:
        return None
    return {
        "tracker_id": row[0], "folder_name": row[1],
        "folder_id": row[2], "folder_url": row[3],
        "chapter_count": row[4],
    }


async def tdf_upsert(tracker_id: int, folder_name: str, folder_id: str = "", folder_url: str = "") -> None:
    db = await _get_db()
    await db.execute(
        "INSERT INTO tracker_drive_folders (tracker_id, folder_name, folder_id, folder_url) "
        "VALUES (?,?,?,?) ON CONFLICT(tracker_id) DO UPDATE SET "
        "folder_id=excluded.folder_id, folder_url=excluded.folder_url",
        (tracker_id, folder_name, folder_id, folder_url),
    )
    await db.commit()


async def tdf_increment(tracker_id: int) -> None:
    db = await _get_db()
    await db.execute(
        "UPDATE tracker_drive_folders SET chapter_count = chapter_count + 1 WHERE tracker_id=?",
        (tracker_id,),
    )
    await db.commit()


# ── extract_chunks_cache CRUD ───────────────────────────────────────────────

async def get_cached_chunk(folder_id: str, lang: str, chunk_idx: int) -> dict | None:
    db = await _get_db()
    async with db.execute(
        "SELECT data FROM extract_chunks_cache WHERE folder_id=? AND lang=? AND chunk_idx=?",
        (folder_id, lang, chunk_idx)
    ) as c:
        row = await c.fetchone()
    if row:
        try:
            return json.loads(row[0])
        except Exception:
            pass
    return None


async def save_cached_chunk(folder_id: str, lang: str, chunk_idx: int, data: dict) -> None:
    db = await _get_db()
    await db.execute(
        "INSERT INTO extract_chunks_cache (folder_id, lang, chunk_idx, data) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(folder_id, lang, chunk_idx) DO UPDATE SET "
        "data=excluded.data, updated_at=CURRENT_TIMESTAMP",
        (folder_id, lang, chunk_idx, json.dumps(data))
    )
    await db.commit()


async def clear_folder_cache(folder_id: str) -> None:
    db = await _get_db()
    await db.execute("DELETE FROM extract_chunks_cache WHERE folder_id=?", (folder_id,))
    await db.commit()


# ── User Limits & Credits CRUD ───────────────────────────────────────────────

async def get_user_credits(user_id: int) -> dict:
    db = await _get_db()
    async with db.execute(
        "SELECT inpainting_credits, extraction_credits, used_trial_clean, used_trial_extract, vip_expiry "
        "FROM user_permissions WHERE user_id = ?",
        (user_id,)
    ) as c:
        row = await c.fetchone()
    if row:
        return {
            "inpainting_credits": row[0],
            "extraction_credits": row[1],
            "used_trial_clean": row[2],
            "used_trial_extract": row[3],
            "vip_expiry": row[4]
        }
    return {
        "inpainting_credits": 0,
        "extraction_credits": 0,
        "used_trial_clean": 0,
        "used_trial_extract": 0,
        "vip_expiry": None
    }

async def add_user_credits(user_id: int, inpainting: int = 0, extraction: int = 0) -> None:
    db = await _get_db()
    # Register user first if not exists
    await db.execute(
        "INSERT OR IGNORE INTO user_permissions (user_id, rank, note) VALUES (?, 1, 'Auto-registered')",
        (user_id,)
    )
    await db.execute(
        "UPDATE user_permissions SET inpainting_credits = MAX(0, inpainting_credits + ?), "
        "extraction_credits = MAX(0, extraction_credits + ?) WHERE user_id = ?",
        (inpainting, extraction, user_id)
    )
    await db.commit()


async def log_credit_transaction(from_user: int, to_user: int, credit_type: str, amount: int, reason: str = "") -> None:
    db = await _get_db()
    await db.execute(
        "INSERT INTO credit_transactions (from_user, to_user, credit_type, amount, reason) VALUES (?, ?, ?, ?, ?)",
        (from_user, to_user, credit_type, amount, reason),
    )
    await db.commit()


async def transfer_user_credits(from_user: int, to_user: int, credit_type: str, amount: int) -> tuple[bool, str]:
    if amount <= 0:
        return False, "❌ يجب أن يكون عدد النقاط أكبر من صفر."
    if from_user == to_user:
        return False, "❌ لا يمكنك إهداء النقاط لنفسك."
    
    sender_credits = await get_user_credits(from_user)
    field = "inpainting_credits" if credit_type == "clean" else "extraction_credits"
    if sender_credits.get(field, 0) < amount:
        type_name = "التبييض" if credit_type == "clean" else "الاستخراج"
        return False, f"❌ ليس لديك رصيد كافٍ من نقاط {type_name} للإهداء (المتاح: {sender_credits.get(field, 0)})."

    if credit_type == "clean":
        await add_user_credits(from_user, inpainting=-amount)
        await add_user_credits(to_user, inpainting=amount)
    else:
        await add_user_credits(from_user, extraction=-amount)
        await add_user_credits(to_user, extraction=amount)

    await log_credit_transaction(from_user, to_user, credit_type, amount, "User Gift")
    return True, f"🎁 تم إهداء `{amount}` نقطة إلى <@{to_user}> بنجاح!"

async def get_user_daily_usage(user_id: int, date_str: str) -> dict:
    db = await _get_db()
    async with db.execute(
        "SELECT clean_count, extract_count FROM user_daily_usage WHERE user_id = ? AND usage_date = ?",
        (user_id, date_str)
    ) as c:
        row = await c.fetchone()
    if row:
        return {
            "clean_count": row[0],
            "extract_count": row[1]
        }
    return {
        "clean_count": 0,
        "extract_count": 0
    }

async def increment_user_daily_usage(user_id: int, date_str: str, usage_type: str) -> None:
    db = await _get_db()
    if usage_type == "clean":
        sql = "INSERT INTO user_daily_usage (user_id, usage_date, clean_count) VALUES (?, ?, 1) " \
              "ON CONFLICT(user_id, usage_date) DO UPDATE SET clean_count = clean_count + 1"
    else:
        sql = "INSERT INTO user_daily_usage (user_id, usage_date, extract_count) VALUES (?, ?, 1) " \
              "ON CONFLICT(user_id, usage_date) DO UPDATE SET extract_count = extract_count + 1"
    await db.execute(sql, (user_id, date_str))
    await db.commit()

async def set_user_trial_used(user_id: int, usage_type: str) -> None:
    db = await _get_db()
    # Register user first if not exists
    await db.execute(
        "INSERT OR IGNORE INTO user_permissions (user_id, rank, note) VALUES (?, 1, 'Auto-registered')",
        (user_id,)
    )
    if usage_type == "clean":
        sql = "UPDATE user_permissions SET used_trial_clean = 1 WHERE user_id = ?"
    else:
        sql = "UPDATE user_permissions SET used_trial_extract = 1 WHERE user_id = ?"
    await db.execute(sql, (user_id,))
    await db.commit()


async def reset_all_daily_usage() -> None:
    db = await _get_db()
    await db.execute("DELETE FROM user_daily_usage")
    await db.commit()


# ── User Extraction Settings CRUD ─────────────────────────────────────────────

async def get_user_profiles(user_id: int) -> list[dict]:
    db = await _get_db()
    async with db.execute("SELECT COUNT(*) FROM user_extraction_settings WHERE user_id = ?", (user_id,)) as c:
        row = await c.fetchone()
        count = row[0] if row else 0
    if count == 0:
        await db.execute(
            "INSERT INTO user_extraction_settings (user_id, profile_name, is_active) VALUES (?, 'Default', 1)",
            (user_id,)
        )
        await db.commit()
    
    profiles = []
    async with db.execute(
        "SELECT profile_name, add_spaces, remove_sfx, remove_legends, connected_slashes, output_format, model_mode, "
        "legend_speech, legend_shouting, legend_small, legend_thinking, legend_box, legend_system, legend_outer, legend_sfx, is_active "
        "FROM user_extraction_settings WHERE user_id = ? ORDER BY profile_name = 'Default' DESC, profile_name ASC",
        (user_id,)
    ) as c:
        async for row in c:
            profiles.append({
                "profile_name": row[0],
                "add_spaces": row[1],
                "remove_sfx": row[2],
                "remove_legends": row[3],
                "connected_slashes": row[4],
                "output_format": row[5],
                "model_mode": row[6],
                "legend_speech": row[7],
                "legend_shouting": row[8],
                "legend_small": row[9],
                "legend_thinking": row[10],
                "legend_box": row[11],
                "legend_system": row[12],
                "legend_outer": row[13],
                "legend_sfx": row[14],
                "is_active": row[15]
            })
    return profiles

async def get_active_user_settings(user_id: int) -> dict:
    db = await _get_db()
    async with db.execute(
        "SELECT add_spaces, remove_sfx, remove_legends, connected_slashes, output_format, model_mode, "
        "legend_speech, legend_shouting, legend_small, legend_thinking, legend_box, legend_system, legend_outer, legend_sfx, profile_name "
        "FROM user_extraction_settings WHERE user_id = ? AND is_active = 1",
        (user_id,)
    ) as c:
        row = await c.fetchone()
    if row:
        return {
            "add_spaces": row[0],
            "remove_sfx": row[1],
            "remove_legends": row[2],
            "connected_slashes": row[3],
            "output_format": row[4],
            "model_mode": row[5],
            "legend_speech": row[6],
            "legend_shouting": row[7],
            "legend_small": row[8],
            "legend_thinking": row[9],
            "legend_box": row[10],
            "legend_system": row[11],
            "legend_outer": row[12],
            "legend_sfx": row[13],
            "profile_name": row[14]
        }
    profiles = await get_user_profiles(user_id)
    for p in profiles:
        if p["is_active"]:
            return p
    return profiles[0] if profiles else {}

async def save_user_settings(user_id: int, profile_name: str, settings: dict) -> None:
    db = await _get_db()
    sql = """
        UPDATE user_extraction_settings SET
            add_spaces = ?,
            remove_sfx = ?,
            remove_legends = ?,
            connected_slashes = ?,
            output_format = ?,
            model_mode = ?,
            legend_speech = ?,
            legend_shouting = ?,
            legend_small = ?,
            legend_thinking = ?,
            legend_box = ?,
            legend_system = ?,
            legend_outer = ?,
            legend_sfx = ?
        WHERE user_id = ? AND profile_name = ?
    """
    await db.execute(sql, (
        settings.get("add_spaces", 1),
        settings.get("remove_sfx", 1),
        settings.get("remove_legends", 0),
        settings.get("connected_slashes", 0),
        settings.get("output_format", 'BOTH'),
        settings.get("model_mode", 'ADVANCED'),
        settings.get("legend_speech", '""'),
        settings.get("legend_shouting", '::'),
        settings.get("legend_small", 'ST'),
        settings.get("legend_thinking", '()'),
        settings.get("legend_box", '[]'),
        settings.get("legend_system", '<>'),
        settings.get("legend_outer", 'OT'),
        settings.get("legend_sfx", 'SFX'),
        user_id,
        profile_name
    ))
    await db.commit()

async def create_user_profile(user_id: int, profile_name: str) -> bool:
    db = await _get_db()
    async with db.execute("SELECT COUNT(*) FROM user_extraction_settings WHERE user_id = ?", (user_id,)) as c:
        row = await c.fetchone()
        count = row[0] if row else 0
    if count >= 5:
        return False
    
    async with db.execute(
        "SELECT add_spaces, remove_sfx, remove_legends, connected_slashes, output_format, model_mode, "
        "legend_speech, legend_shouting, legend_small, legend_thinking, legend_box, legend_system, legend_outer, legend_sfx "
        "FROM user_extraction_settings WHERE user_id = ? AND profile_name = 'Default'",
        (user_id,)
    ) as c:
        row = await c.fetchone()
    
    if row:
        sql = """
            INSERT INTO user_extraction_settings (
                user_id, profile_name, add_spaces, remove_sfx, remove_legends, connected_slashes,
                output_format, model_mode, legend_speech, legend_shouting, legend_small,
                legend_thinking, legend_box, legend_system, legend_outer, legend_sfx, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """
        await db.execute(sql, (user_id, profile_name, *row))
    else:
        sql = """
            INSERT INTO user_extraction_settings (
                user_id, profile_name, is_active
            ) VALUES (?, ?, 0)
        """
        await db.execute(sql, (user_id, profile_name))
    
    await db.commit()
    return True

async def set_active_profile(user_id: int, profile_name: str) -> None:
    db = await _get_db()
    await db.execute("UPDATE user_extraction_settings SET is_active = 0 WHERE user_id = ?", (user_id,))
    await db.execute("UPDATE user_extraction_settings SET is_active = 1 WHERE user_id = ? AND profile_name = ?", (user_id, profile_name))
    await db.commit()


async def set_user_vip_expiry(user_id: int, expiry_iso: str | None) -> None:
    db = await _get_db()
    await db.execute(
        "INSERT OR IGNORE INTO user_permissions (user_id, rank, note) VALUES (?, 1, 'Auto-registered')",
        (user_id,)
    )
    await db.execute(
        "UPDATE user_permissions SET vip_expiry = ? WHERE user_id = ?",
        (expiry_iso, user_id)
    )
    await db.commit()


# ── Tracker Subscriptions & Heal System Helpers ─────────────────────────────────

async def toggle_user_subscription(tracker_id: int, user_id: int) -> bool:
    """Toggles personal notification subscription for user_id on tracker_id. Returns True if now subscribed, False if unsubscribed."""
    db = await _get_db()
    async with db.execute(
        "SELECT 1 FROM tracker_subscriptions WHERE tracker_id = ? AND user_id = ?",
        (tracker_id, user_id)
    ) as cursor:
        row = await cursor.fetchone()
    if row:
        await db.execute(
            "DELETE FROM tracker_subscriptions WHERE tracker_id = ? AND user_id = ?",
            (tracker_id, user_id)
        )
        await db.commit()
        return False
    else:
        await db.execute(
            "INSERT INTO tracker_subscriptions (tracker_id, user_id) VALUES (?, ?)",
            (tracker_id, user_id)
        )
        await db.commit()
        return True


async def get_series_subscribers(tracker_id: int) -> list[int]:
    """Returns list of user IDs subscribed to notifications for tracker_id."""
    db = await _get_db()
    async with db.execute(
        "SELECT user_id FROM tracker_subscriptions WHERE tracker_id = ?",
        (tracker_id,)
    ) as cursor:
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def is_user_subscribed(tracker_id: int, user_id: int) -> bool:
    """Checks whether user_id is subscribed to tracker_id."""
    db = await _get_db()
    async with db.execute(
        "SELECT 1 FROM tracker_subscriptions WHERE tracker_id = ? AND user_id = ?",
        (tracker_id, user_id)
    ) as cursor:
        return (await cursor.fetchone()) is not None


async def heal_system() -> dict[str, str | bool | int]:
    """
    Executes a comprehensive system health diagnostic & repair routine:
    1. Verifies DB integrity via PRAGMA quick_check.
    2. Clears stale log entries & temporary cache items.
    3. Repairs orphaned tracker states & orphan events.
    4. Cleans expired radar reminders and invalid records.
    Returns a status dict summarizing diagnostic results.
    """
    db = await _get_db()
    results = {
        "db_ok": False,
        "db_check_result": "Unknown",
        "stale_logs_cleared": 0,
        "orphaned_events_repaired": 0,
        "orphaned_reminders_cleared": 0,
        "orphaned_cards_cleared": 0,
        "repaired_trackers_count": 0,
    }

    # 1. PRAGMA quick_check
    try:
        async with db.execute("PRAGMA quick_check") as cursor:
            row = await cursor.fetchone()
            chk_str = row[0] if row else "ok"
            results["db_check_result"] = chk_str
            results["db_ok"] = (str(chk_str).lower() == "ok")
    except Exception as exc:
        results["db_check_result"] = f"Error: {exc}"
        results["db_ok"] = False

    # 2. Clear stale logs (keep last 500)
    try:
        cursor = await db.execute("DELETE FROM bot_logs WHERE id NOT IN (SELECT id FROM bot_logs ORDER BY id DESC LIMIT 500)")
        results["stale_logs_cleared"] = cursor.rowcount or 0
        await db.commit()
    except Exception:
        pass

    # 3. Clean orphaned tracker events
    try:
        cursor = await db.execute(
            "DELETE FROM tracker_events WHERE tracker_id NOT IN (SELECT id FROM server_trackers)"
        )
        results["orphaned_events_repaired"] = cursor.rowcount or 0
        await db.commit()
    except Exception:
        pass

    # 4. Clean orphaned radar reminders
    try:
        cursor = await db.execute(
            "DELETE FROM radar_reminders WHERE fired = 1 OR notify_at < datetime('now', '-7 days')"
        )
        results["orphaned_reminders_cleared"] = cursor.rowcount or 0
        await db.commit()
    except Exception:
        pass

    # 5. Clean orphaned tracker cards
    try:
        cursor = await db.execute(
            "DELETE FROM tracker_cards WHERE tracker_id NOT IN (SELECT id FROM server_trackers) AND tracker_id NOT IN (SELECT id FROM user_trackers)"
        )
        results["orphaned_cards_cleared"] = cursor.rowcount or 0
        await db.commit()
    except Exception:
        pass

    # 6. Repair invalid/negative chapter states or null fields on server_trackers & user_trackers
    try:
        c1 = await db.execute("UPDATE server_trackers SET last_chapter = 0.0 WHERE last_chapter IS NULL OR last_chapter < 0")
        c2 = await db.execute("UPDATE user_trackers SET last_chapter = 0.0 WHERE last_chapter IS NULL OR last_chapter < 0")
        results["repaired_trackers_count"] = (c1.rowcount or 0) + (c2.rowcount or 0)
        await db.commit()
    except Exception:
        pass

    return results


