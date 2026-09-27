"""
rss_watcher.py — Real-time RSS / API watcher for server tracker v3.

Checks supported sites every 30-60s via RSS feeds or native APIs.
Falls back gracefully when site-specific parsing fails.
"""
from __future__ import annotations

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import aiohttp

if TYPE_CHECKING:
    pass

logger = logging.getLogger("sv3.rss_watcher")

# ── Site configs ──────────────────────────────────────────────────────────────

RSS_FEEDS: dict[str, str] = {
    "asuracomic.net":   "https://asuracomic.net/feed/",
    "asuratoon.com":    "https://asuratoon.com/feed/",
    "asurascans.com":   "https://asurascans.com/feed/",
    "shinigami.asia":   "https://g.shinigami.asia/feed/",
    "shngm.net":        "https://shngm.net/feed/",
    "lekmanga.net":     "https://lekmanga.net/feed/",
    "lmscans.net":      "https://lmscans.net/feed/",
    "mangabuff.net":    "https://mangabuff.net/feed/",
    "manganato.com":    "https://manganato.com/sitemap/",
    "webtoons.com":     "https://www.webtoons.com/rss/top.rss",
}

# مواقع تدعم فحص API مباشر (أسرع من RSS)
API_SUPPORTED = {"mangadex.org", "comick.fun", "comick.io", "comick.cc"}

# الحد الأدنى بين فحصين لنفس الـ domain (ثانية)
DOMAIN_MIN_INTERVAL: dict[str, float] = {
    "mangadex.org":   15.0,
    "comick.fun":     30.0,
    "asuracomic.net": 30.0,
    "shinigami.asia": 30.0,
    "lekmanga.net":   45.0,
    "default":        60.0,
}


class RSSEntry:
    __slots__ = ("title", "link", "pub_date", "series_url")

    def __init__(self, title: str, link: str, pub_date: str, series_url: str = ""):
        self.title = title
        self.link = link
        self.pub_date = pub_date
        self.series_url = series_url


class RSSWatcher:
    """
    يراقب feeds من مواقع متعددة ويُشعر عند وجود فصل جديد.
    يعمل بشكل مستقل في الخلفية — يُنبّه TrackerEngineV3 عبر asyncio.Queue.
    """

    def __init__(self, notification_queue: asyncio.Queue):
        self._queue = notification_queue
        self._session: aiohttp.ClientSession | None = None
        self._last_check: dict[str, float] = {}        # domain → timestamp
        self._seen_links: dict[str, set[str]] = {}     # feed_url → set of seen links
        self._task: asyncio.Task | None = None
        self._running = False

    # ── Session management ────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=20)
            connector = aiohttp.TCPConnector(
                limit=10, limit_per_host=3, enable_cleanup_closed=True
            )
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={"User-Agent": "MangaSystem-TrackerBot/3.0 (+https://github.com/mangasystem)"},
            )
        return self._session

    async def close(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """ابدأ مهمة المراقبة في الخلفية."""
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="sv3-rss-watcher")
        logger.info("RSSWatcher started")

    def detect_check_method(self, url: str) -> str:
        """يحدد أفضل طريقة فحص للـ URL تلقائياً."""
        domain = urlparse(url).netloc.lower().replace("www.", "")
        if "mangadex.org" in domain:
            return "mangadex_api"
        if any(d in domain for d in ("comick.fun", "comick.io", "comick.cc")):
            return "comick_api"
        if any(d in domain for d in RSS_FEEDS):
            return "rss"
        return "scrape"

    def is_rss_domain(self, url: str) -> bool:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        return any(d in domain for d in RSS_FEEDS)

    def get_feed_url(self, series_url: str) -> str | None:
        domain = urlparse(series_url).netloc.lower().replace("www.", "")
        for d, feed in RSS_FEEDS.items():
            if d in domain:
                return feed
        return None

    # ── Throttle helper ───────────────────────────────────────────────────────

    def _can_check(self, domain: str) -> bool:
        min_int = DOMAIN_MIN_INTERVAL.get(domain, DOMAIN_MIN_INTERVAL["default"])
        last = self._last_check.get(domain, 0)
        return (time.time() - last) >= min_int

    def _mark_checked(self, domain: str) -> None:
        self._last_check[domain] = time.time()

    # ── Core loop ─────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        """الحلقة الرئيسية — تفحص كل RSS feed مرة كل دقيقة."""
        await asyncio.sleep(5)  # انتظر يبدأ البوت
        while self._running:
            try:
                for domain, feed_url in RSS_FEEDS.items():
                    if not self._running:
                        break
                    if not self._can_check(domain):
                        continue
                    try:
                        entries = await self._fetch_rss(feed_url)
                        new_entries = self._filter_new(feed_url, entries)
                        if new_entries:
                            logger.info(f"[RSS] {domain}: {len(new_entries)} new entries")
                            for entry in new_entries:
                                await self._queue.put(("rss_entry", domain, entry))
                        self._mark_checked(domain)
                    except Exception as e:
                        logger.debug(f"[RSS] Failed to check {domain}: {e}")

                await asyncio.sleep(20)  # استرح 20 ثانية بين الجولات
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[RSS] Loop error: {e}")
                await asyncio.sleep(30)

    def _filter_new(self, feed_url: str, entries: list[RSSEntry]) -> list[RSSEntry]:
        """يرجع فقط الإدخالات التي لم نرها قبل."""
        seen = self._seen_links.setdefault(feed_url, set())
        new = [e for e in entries if e.link not in seen]
        # حفظ الجديدة
        for e in new:
            seen.add(e.link)
        # تنظيف: احتفظ بآخر 500 رابط فقط
        if len(seen) > 500:
            self._seen_links[feed_url] = set(list(seen)[-500:])
        return new

    # ── RSS Fetching ──────────────────────────────────────────────────────────

    async def _fetch_rss(self, feed_url: str) -> list[RSSEntry]:
        session = await self._get_session()
        async with session.get(feed_url) as resp:
            if resp.status != 200:
                return []
            text = await resp.text(encoding="utf-8", errors="replace")
        return self._parse_rss(text)

    def _parse_rss(self, xml_text: str) -> list[RSSEntry]:
        """يحلّل XML/RSS ويرجع قائمة RSSEntry."""
        entries = []
        try:
            root = ET.fromstring(xml_text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}

            # RSS 2.0
            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                link  = (item.findtext("link")  or "").strip()
                date  = (item.findtext("pubDate") or "").strip()
                if link:
                    entries.append(RSSEntry(title, link, date))

            # Atom
            if not entries:
                for entry in root.findall("atom:entry", ns):
                    title = (entry.findtext("atom:title", namespaces=ns) or "").strip()
                    link_el = entry.find("atom:link", ns)
                    link = (link_el.get("href") or "") if link_el is not None else ""
                    date = (entry.findtext("atom:updated", namespaces=ns) or "").strip()
                    if link:
                        entries.append(RSSEntry(title, link, date))
        except ET.ParseError as e:
            logger.debug(f"RSS parse error: {e}")
        return entries

    # ── MangaDex API check ────────────────────────────────────────────────────

    async def check_mangadex(self, series_url: str, last_chapter: float) -> dict | None:
        """فحص سريع لـ MangaDex عبر API."""
        import re
        m = re.search(r"/title/([a-f0-9\-]{36})", series_url)
        if not m:
            return None
        manga_id = m.group(1)
        try:
            session = await self._get_session()
            params = {
                "manga[]": manga_id,
                "limit": 5,
                "order[chapter]": "desc",
                "translatedLanguage[]": ["en", "ar"],
            }
            async with session.get("https://api.mangadex.org/chapter", params=params) as r:
                if r.status != 200:
                    return None
                data = await r.json()
            items = data.get("data", [])
            if not items:
                return None
            latest_item = items[0]
            attrs = latest_item.get("attributes", {})
            ch_str = attrs.get("chapter") or "0"
            try:
                ch_num = float(ch_str)
            except ValueError:
                return None
            if ch_num <= last_chapter:
                return None
            ch_id = latest_item["id"]
            return {
                "latest": ch_num,
                "chapter_url": f"https://mangadex.org/chapter/{ch_id}",
                "locked": False,
            }
        except Exception as e:
            logger.debug(f"MangaDex API error: {e}")
            return None

    # ── Comick API check ──────────────────────────────────────────────────────

    async def check_comick(self, series_url: str, last_chapter: float) -> dict | None:
        """فحص سريع لـ Comick عبر API."""
        import re
        m = re.search(r"/(comic|manga)/([^/]+)", series_url)
        if not m:
            return None
        slug = m.group(2)
        try:
            session = await self._get_session()
            async with session.get(f"https://api.comick.fun/comic/{slug}/chapters?limit=5&order=desc") as r:
                if r.status != 200:
                    return None
                data = await r.json()
            chapters = data.get("chapters", data) if isinstance(data, dict) else data
            if not chapters:
                return None
            first = chapters[0]
            ch_num = float(first.get("chap") or 0)
            if ch_num <= last_chapter:
                return None
            hid = first.get("hid", "")
            return {
                "latest": ch_num,
                "chapter_url": f"https://comick.fun/chapter/{hid}",
                "locked": False,
            }
        except Exception as e:
            logger.debug(f"Comick API error: {e}")
            return None
