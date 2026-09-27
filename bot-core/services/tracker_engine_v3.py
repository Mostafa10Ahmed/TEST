"""
tracker_engine_v3.py — Core background engine for server tracker v3.

Architecture:
  ┌─────────────────────────────────────────────────────┐
  │                  TrackerEngineV3                    │
  │                                                     │
  │  RSSWatcher ──► notification_queue ──► _dispatcher  │
  │                                           │         │
  │  _scrape_loop (priority queue) ──────────►│         │
  │                                           ▼         │
  │                              DB insert (anti-dup)   │
  │                                           │         │
  │                              NotifierV3.enqueue()   │
  │                                           │         │
  │                              DLQueue.enqueue()      │
  └─────────────────────────────────────────────────────┘

Key guarantees:
  1. tevt_try_insert() → UNIQUE constraint → no duplicate events
  2. asyncio.Lock per URL → no concurrent scrapes of same URL
  3. DLQueue semaphore → max 3 concurrent downloads
  4. auto-pause after MAX_FAILURES consecutive failures
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import aiohttp

import database
from services.rss_watcher import RSSWatcher
from services.notifier_v3 import NotifierV3
from services.dl_queue import DLQueue

if TYPE_CHECKING:
    from discord.ext.commands import Bot
    from remote_downloader import RemoteDownloader

logger = logging.getLogger("sv3.engine")

MAX_FAILURES      = 5       # أوقف التتبع تلقائياً بعد كذا فشل متتالي
SCRAPE_BATCH      = 10      # كم تراكر يُفحص بشكل متوازٍ في كل دورة
LOOP_SLEEP        = 60      # ثانية بين دورات الـ scrape fallback
COVER_CACHE_SIZE  = 512     # أقصى عدد covers مكاشَة


is_main_chapter = database.is_main_chapter
should_ignore_chapter = database.should_ignore_chapter


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return url


def _slug_to_name(url: str) -> str:
    """استخراج اسم السلسلة من الرابط."""
    if "?" in url:
        url = url.split("?")[0]
    parts = [p for p in url.rstrip("/").split("/") if p]
    skip = {"status", "detail", "chapters", "list", "webtoon", "manga", "series", "comic"}
    while parts and parts[-1].lower() in skip:
        parts.pop()
    if not parts:
        return "Series"
    slug = parts[-1]
    if re.search(r"chapter|ch-?\d|episode|ep-?\d", slug, re.I) and len(parts) > 1:
        slug = parts[-2]
    name = slug.replace("-", " ").replace("_", " ").title()
    name = re.sub(r"\s+[0-9a-f]{6,}\s*$", "", name, flags=re.I).strip()
    return name or "Series"


class TrackerEngineV3:
    """
    المحرك المركزي لنظام التتبع v3.
    يُشغَّل مرة واحدة عند بدء البوت ويعمل بشكل مستمر في الخلفية.
    """

    def __init__(self, bot: "Bot", remote_down: "RemoteDownloader"):
        self.bot = bot
        self.remote_down = remote_down

        # الطابور المشترك بين RSSWatcher والـ engine
        self._notif_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

        # الخدمات
        self.rss_watcher = RSSWatcher(self._notif_queue)
        self.notifier    = NotifierV3(bot)
        self.dl_queue    = DLQueue(remote_down, self.notifier, bot)

        # Lock لكل URL — يمنع scraping متزامن لنفس الرابط
        self._url_locks: dict[str, asyncio.Lock] = {}

        # Cache للـ covers (title → cover_url)
        self._cover_cache: dict[str, str] = {}

        # Cache للـ latest chapters من الـ scraping
        self._latest_cache: dict[str, tuple[float, str]] = {}  # url → (num, ch_url)

        self._tasks: list[asyncio.Task] = []
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.notifier.start()
        self.dl_queue.start()
        self.rss_watcher.start()
        self._tasks = [
            asyncio.create_task(self._rss_dispatcher(), name="sv3-rss-dispatcher"),
            asyncio.create_task(self._scrape_loop(),    name="sv3-scrape-loop"),
            asyncio.create_task(self._maintenance_loop(), name="sv3-maintenance"),
        ]
        logger.info("TrackerEngineV3 started ✅")

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await self.rss_watcher.close()
        await self.notifier.close()
        await self.dl_queue.close()
        logger.info("TrackerEngineV3 stopped")

    async def startup_recovery(self) -> None:
        """يُنفَّذ مرة واحدة عند بدء البوت لاستئناف التحميلات المعلقة."""
        count = await self.dl_queue.recover_pending()
        if count:
            logger.info(f"[Engine] Recovered {count} pending downloads from crash")

    # ── RSS Dispatcher ────────────────────────────────────────────────────────

    async def _rss_dispatcher(self) -> None:
        """
        يستقبل أحداث RSS ويقابلها مع التراكرز المسجلة.
        يعمل كـ fan-out: حدث RSS واحد → N تراكرز مطابقة.
        """
        while self._running:
            try:
                item = await asyncio.wait_for(self._notif_queue.get(), timeout=5)
                kind = item[0]
                if kind == "rss_entry":
                    _, domain, entry = item
                    await self._handle_rss_entry(domain, entry)
                self._notif_queue.task_done()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Engine] Dispatcher error: {e}", exc_info=True)

    async def _handle_rss_entry(self, domain: str, entry) -> None:
        """يبحث عن التراكرز التي تطابق رابط الـ RSS entry ويُعالجها."""
        # Extract chapter number from RSS link/title
        ch_num = _extract_chapter_num(entry.link, entry.title)
        if ch_num is None:
            return

        # ابحث عن كل التراكرز التي تتبع هذا الـ domain
        all_trackers = await database.sv3_all_active()
        matched = [
            t for t in all_trackers
            if domain in _domain(t["url"])
            and _urls_same_series(entry.link, t["url"])
        ]

        for tracker in matched:
            if ch_num <= tracker.get("last_chapter", 0):
                continue
            await self._process_new_chapter(tracker, ch_num, entry.link)

    # ── Scrape Loop ───────────────────────────────────────────────────────────

    async def _scrape_loop(self) -> None:
        """
        Fallback scraping للمواقع التي لا تدعم RSS.
        يستخدم PriorityQueue: heat_score عالي = فحص أكثر.
        """
        await asyncio.sleep(10)  # انتظر يبدأ البوت
        while self._running:
            try:
                trackers = await database.sv3_all_active()
                # رتّب حسب last_checked (الأقدم فحصاً = أولوية أعلى)
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                due = [
                    t for t in trackers
                    if not self.rss_watcher.is_rss_domain(t["url"])
                    and self._is_due(t, now_iso)
                ]

                if due:
                    logger.debug(f"[Engine] Scrape: {len(due)} due trackers")

                # فحص بدفعات متوازية
                for i in range(0, len(due), SCRAPE_BATCH):
                    batch = due[i: i + SCRAPE_BATCH]
                    await asyncio.gather(
                        *[self._scrape_tracker(t) for t in batch],
                        return_exceptions=True,
                    )
                    await asyncio.sleep(1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Engine] Scrape loop error: {e}", exc_info=True)

            await asyncio.sleep(LOOP_SLEEP)

    def _is_due(self, tracker: dict, now_iso: str) -> bool:
        """هل حان وقت فحص هذا التراكر؟"""
        last = tracker.get("last_checked") or ""
        if not last:
            return True
        try:
            last_dt = datetime.datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=datetime.timezone.utc)
            # حسب heat_score: high = كل 10 دقائق، low = كل ساعة
            heat = tracker.get("heat_score", 50.0)
            interval_mins = max(5, int(70 - heat * 0.6))  # 5 → 70 min
            delta = datetime.timedelta(minutes=interval_mins)
            return datetime.datetime.now(datetime.timezone.utc) - last_dt >= delta
        except Exception:
            return True

    async def _scrape_tracker(self, tracker: dict) -> None:
        """يفحص تراكراً واحداً عبر الـ provider_mgr."""
        url = tracker["url"]
        tid = tracker["id"]
        guild_id = tracker["guild_id"]
        title = tracker.get("title")

        lock = self._url_locks.setdefault(url, asyncio.Lock())
        if lock.locked():
            return  # يُفحص الآن من thread آخر

        async with lock:
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            await database.sv3_update(tid, guild_id, last_checked=now_iso)
            try:
                result = await self._fetch_latest(url, tracker_title=title)
                if result is None:
                    await self._increment_failure(tracker)
                    return

                latest_num, latest_url = result
                # Reset failure counter on success
                if tracker.get("consecutive_failures", 0) > 0:
                    await database.sv3_update(tid, guild_id, consecutive_failures=0)

                if latest_num > tracker.get("last_chapter", 0):
                    await self._process_new_chapter(tracker, latest_num, latest_url)

            except Exception as e:
                logger.warning(f"[Engine] Scrape error for {url}: {e}")
                await self._increment_failure(tracker)

    async def _fetch_latest(self, url: str, tracker_title: str | None = None) -> tuple[float, str] | None:
        """يجلب آخر فصل للسلسلة — يجرب API أولاً ثم scraping ثم Multi-Source Fallback."""
        domain = _domain(url)

        # MangaDex
        if "mangadex.org" in domain:
            result = await self.rss_watcher.check_mangadex(url, 0)
            if result:
                return result["latest"], result["chapter_url"]

        # Comick
        if any(d in domain for d in ("comick.fun", "comick.io", "comick.cc")):
            result = await self.rss_watcher.check_comick(url, 0)
            if result:
                return result["latest"], result["chapter_url"]

        # Generic scraping via provider_mgr
        pm = getattr(self.bot, "provider_mgr", None)
        if pm is None:
            return None
        try:
            chapters = None
            if hasattr(pm, "get_all_chapters"):
                chapters = await asyncio.wait_for(
                    pm.get_all_chapters(url), timeout=30
                )
            elif hasattr(pm, "get_chapters"):
                chapters = await asyncio.wait_for(
                    pm.get_chapters(url), timeout=30
                )

            if chapters:
                if isinstance(chapters, dict):
                    latest_num = max(chapters.keys())
                    val = chapters[latest_num]
                    ch_url = val if isinstance(val, str) else val.get("url", url)
                    return (float(latest_num), str(ch_url))
                elif isinstance(chapters, list):
                    latest = max(chapters, key=lambda c: float(c.get("num", c.get("chapter_num", 0)) or 0))
                    num = float(latest.get("num", latest.get("chapter_num", 0)) or 0)
                    ch_url = latest.get("url", latest.get("chapter_url", url)) or url
                    return (num, str(ch_url)) if num > 0 else None
        except asyncio.TimeoutError:
            logger.debug(f"[Engine] Timeout scraping {url}")
        except Exception as e:
            logger.debug(f"[Engine] Scraping error for {url}: {e}")

        # Multi-Source Fallback Engine if primary source returned empty / failed
        if hasattr(pm, "get_all_chapters_with_fallback"):
            try:
                fallback_chapters = await pm.get_all_chapters_with_fallback(url, series_title=tracker_title)
                if fallback_chapters and isinstance(fallback_chapters, dict):
                    latest_num = max(fallback_chapters.keys())
                    val = fallback_chapters[latest_num]
                    ch_url = val if isinstance(val, str) else val.get("url", url)
                    return (float(latest_num), str(ch_url))
            except Exception as fe:
                logger.debug(f"[Engine] Multi-source fallback failed for {url}: {fe}")

        return None

    # ── Core: process new chapter ─────────────────────────────────────────────

    async def _process_new_chapter(
        self,
        tracker: dict,
        chapter_num: float,
        chapter_url: str,
    ) -> None:
        """
        الخط الرئيسي عند اكتشاف فصل جديد:
        1. Custom Chapter Filtering (ignore_sub_chapters check)
        2. tevt_try_insert → anti-duplicate layer 1
        3. إشعار → NotifierV3
        4. تحميل تلقائي → DLQueue
        5. تحديث last_chapter في DB
        """
        tid      = tracker["id"]
        guild_id = tracker["guild_id"]

        # ── Custom Chapter Filtering ─────────────────────────────────────
        ignore_sub = bool(tracker.get("ignore_sub_chapters", 0))
        if should_ignore_chapter(chapter_num, ignore_sub_chapters=ignore_sub):
            logger.info(
                f"[Engine] Ignoring fractional sub-chapter Ch.{chapter_num} for tracker #{tid} "
                f"(ignore_sub_chapters=True)"
            )
            await database.sv3_update(tid, guild_id, last_chapter=chapter_num)
            return

        # ── Layer 1 anti-duplicate: DB UNIQUE insert ───────────────────────
        is_new = await database.tevt_try_insert(tid, chapter_num, chapter_url)
        if not is_new:
            logger.debug(f"[Engine] Chapter already registered: #{tid} ch{chapter_num}")
            return

        # تحديث last_chapter مبكراً لمنع إعادة الفحص
        await database.sv3_update(tid, guild_id, last_chapter=chapter_num)

        event = await database.tevt_get(tid, chapter_num)
        if not event:
            return  # shouldn't happen

        # Refresh tracker from DB (قد تغيّر الـ cover_url وغيره)
        tracker = await database.sv3_get(tid, guild_id) or tracker

        # جلب الغلاف لو ما اتجلب قبل
        if not tracker.get("cover_url"):
            cover = await self._fetch_cover(tracker["url"])
            if cover:
                await database.sv3_update(tid, guild_id, cover_url=cover)
                tracker = dict(tracker, cover_url=cover)

        logger.info(
            f"[Engine] 🆕 New chapter: {tracker.get('title') or tid} "
            f"Ch.{chapter_num} → {chapter_url[:60]}"
        )

        # ── إشعار فوري ────────────────────────────────────────────────────
        await self.notifier.enqueue_new_chapter(tracker, event)

        # ── تحميل تلقائي ─────────────────────────────────────────────────
        if tracker.get("auto_download"):
            await self.dl_queue.enqueue(tracker, event)

    # ── Public API: manual check ──────────────────────────────────────────────

    async def check_now(self, tracker_id: int, guild_id: int) -> str:
        """
        يُشغَّل من أمر /tracker check — فحص فوري لتراكر محدد.
        يرجع رسالة نتيجة.
        """
        tracker = await database.sv3_get(tracker_id, guild_id)
        if not tracker:
            return "❌ التراكر غير موجود."
        if tracker.get("paused"):
            return "⏸️ التتبع موقوف مؤقتاً."

        result = await self._fetch_latest(tracker["url"])
        if result is None:
            return "⚠️ تعذر جلب معلومات الفصل من المصدر."

        latest_num, latest_url = result
        if latest_num <= tracker.get("last_chapter", 0):
            return f"✅ لا يوجد تحديث — آخر فصل: **Ch. {latest_num}**"

        await self._process_new_chapter(tracker, latest_num, latest_url)
        return f"🆕 فصل جديد تم اكتشافه: **Ch. {latest_num}** — الإشعار في الطريق!"

    # ── Add tracker helper ────────────────────────────────────────────────────

    async def bootstrap_tracker(self, tracker_id: int, guild_id: int) -> None:
        """
        يُشغَّل مباشرة بعد إضافة تراكر جديد:
        - يجلب الاسم والغلاف تلقائياً
        - يكتشف آخر فصل ويحفظه (بدون إرسال إشعار — هذا "baseline")
        """
        tracker = await database.sv3_get(tracker_id, guild_id)
        if not tracker:
            return

        url = tracker["url"]

        # جلب اسم السلسلة لو لم يُعطَ
        title = tracker.get("title") or _slug_to_name(url)

        # جلب الغلاف
        cover = await self._fetch_cover(url)

        # جلب آخر فصل
        result = await self._fetch_latest(url)
        latest_num = 0.0
        if result:
            latest_num, _ = result

        updates = {"title": title, "last_chapter": latest_num}
        if cover:
            updates["cover_url"] = cover
        # اكتشف طريقة الفحص تلقائياً
        updates["check_method"] = self.rss_watcher.detect_check_method(url)

        await database.sv3_update(tracker_id, guild_id, **updates)
        logger.info(
            f"[Engine] Bootstrap done: #{tracker_id} '{title}' "
            f"ch={latest_num} method={updates['check_method']}"
        )

    # ── Maintenance ───────────────────────────────────────────────────────────

    async def _maintenance_loop(self) -> None:
        """تنظيف دوري — يعمل مرة كل 24 ساعة."""
        await asyncio.sleep(3600)
        while self._running:
            try:
                await database.tevt_cleanup_old(days=60)
                logger.info("[Engine] 🧹 Maintenance: cleaned old tracker_events")
            except Exception as e:
                logger.error(f"[Engine] Maintenance error: {e}")
            await asyncio.sleep(86400)

    async def _increment_failure(self, tracker: dict) -> None:
        failures = tracker.get("consecutive_failures", 0) + 1
        updates: dict = {"consecutive_failures": failures}
        if failures >= MAX_FAILURES:
            updates["paused"] = 1
            logger.warning(
                f"[Engine] Auto-paused tracker #{tracker['id']} "
                f"after {failures} consecutive failures"
            )
        await database.sv3_update(tracker["id"], tracker["guild_id"], **updates)

    # ── Cover fetching ────────────────────────────────────────────────────────

    async def _fetch_cover(self, url: str) -> str | None:
        if url in self._cover_cache:
            return self._cover_cache[url]
        pm = getattr(self.bot, "provider_mgr", None)
        if pm is None:
            return None
        try:
            cover = await asyncio.wait_for(pm.get_series_cover(url), timeout=10)
            if cover and str(cover).startswith("http"):
                self._cover_cache[url] = str(cover)
                if len(self._cover_cache) > COVER_CACHE_SIZE:
                    # حذف أقدم مدخل
                    oldest = next(iter(self._cover_cache))
                    del self._cover_cache[oldest]
                return str(cover)
        except Exception:
            pass
        return None


# ── URL matching helpers ──────────────────────────────────────────────────────

def _extract_chapter_num(url: str, title: str = "") -> float | None:
    """يستخرج رقم الفصل من الرابط أو العنوان."""
    # من الرابط
    m = re.search(r"chapter[-_/]?(\d+(?:\.\d+)?)", url, re.I)
    if m:
        return float(m.group(1))
    # من العنوان
    m = re.search(r"ch(?:apter)?\.?\s*(\d+(?:\.\d+)?)", title, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"#(\d+(?:\.\d+)?)", title)
    if m:
        return float(m.group(1))
    return None


def _urls_same_series(rss_link: str, tracker_url: str) -> bool:
    """
    يفحص إذا رابط RSS ورابط التراكر يخصان نفس السلسلة.
    مثال:
      rss_link:    https://asuracomic.net/series/solo-leveling-abc123/chapter-199
      tracker_url: https://asuracomic.net/series/solo-leveling-abc123
    """
    try:
        rss_parsed = urlparse(rss_link)
        trk_parsed = urlparse(tracker_url)

        # لازم نفس الـ domain
        if rss_parsed.netloc != trk_parsed.netloc:
            return False

        # مقارنة الـ path بدون الـ chapter segment
        rss_parts = [p for p in rss_parsed.path.rstrip("/").split("/") if p]
        trk_parts = [p for p in trk_parsed.path.rstrip("/").split("/") if p]

        # حذف chapter segments من نهاية rss_parts
        while rss_parts and re.search(r"chapter|ch-?\d|episode", rss_parts[-1], re.I):
            rss_parts.pop()

        # المقارنة: هل trk_parts موجود في rss_parts؟
        return rss_parts[:len(trk_parts)] == trk_parts
    except Exception:
        return False
