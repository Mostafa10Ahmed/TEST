"""
naver_provider.py — مزود Naver Webtoon/Manhwa

يدعم:
  • m.comic.naver.com  (mobile — الأسرع والأموثق)
  • Pagination كاملة عبر ?page=N
  • كشف الفصول المدفوعة
  • تصفية الأرقام الوهمية (no > 9999)
"""

from __future__ import annotations
import re
import asyncio
from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse
from typing import Optional

import cloudscraper
from bs4 import BeautifulSoup

from .base_provider import BaseProvider

# الحد الأقصى لرقم الفصل الحقيقي
_MAX_CH = 9999

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://comic.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}


def _to_mobile(url: str) -> str:
    """تحويل desktop URL إلى mobile — يعمل أفضل مع cloudscraper."""
    return url.replace("//comic.naver.com", "//m.comic.naver.com")


def _make_page_url(base_url: str, page: int, asc: bool = True) -> str:
    """بناء URL للصفحة رقم page."""
    parsed = urlparse(base_url)
    qs     = parse_qs(parsed.query, keep_blank_values=True)
    qs["page"]      = [str(page)]
    # نستخدم ASC لنضمن ترتيباً ثابتاً (1,2,3,...) وليس عكسياً
    if asc:
        qs["sortOrder"] = ["ASC"]
    new_q = urlencode({k: v[0] for k, v in qs.items()})
    return urlunparse(parsed._replace(query=new_q))


def _extract_from_html(html: str, base_url: str, detect_lock: bool = True) -> dict[float, dict]:
    """
    استخراج الفصول من HTML صفحة Naver.
    يرجع { num: {"url":str, "locked":bool, "reason":str} }
    """
    soup   = BeautifulSoup(html, "html.parser")
    result = {}
    seen   = set()
    
    # Extract title_id from base_url query parameters
    parsed_base = urlparse(base_url)
    qs_base = parse_qs(parsed_base.query)
    url_title_id = qs_base.get("titleId", [None])[0]

    # Try mapping li.item first (new layout style containing data-no)
    for item in soup.select("li.item"):
        data_no = item.get("data-no")
        if not data_no:
            continue
        try:
            num = float(data_no)
        except ValueError:
            continue
            
        if num > _MAX_CH or num in seen:
            continue
        seen.add(num)
        
        title_id = item.get("data-title-id") or url_title_id
        
        # Detect lock
        locked = False
        reason = "free"
        classes = item.get("class", [])
        if "lock" in classes:
            locked = True
            reason = "lock-class"
        else:
            blind_el = item.select_one(".blind")
            if blind_el and "유료만화" in blind_el.get_text():
                locked = True
                reason = "blind-paid"
                
        # Resolve URL
        a_tag = item.select_one("a.link")
        href = a_tag.get("href") if a_tag else None
        if href and href != "#":
            ch_url = urljoin(base_url, href)
        else:
            # Construct standard detail URL for locked chapters
            ch_url = f"https://comic.naver.com/webtoon/detail?titleId={title_id}&no={int(num)}"
            
        result[num] = {"url": ch_url, "locked": locked, "reason": reason}

    if not result:
        # Fallback to old regex-based a-tag extraction
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m    = re.search(r"(?:episode_no|no)=(\d+)", href)
            if not m:
                continue
            num = float(m.group(1))
            # تصفية الأرقام الوهمية
            if num > _MAX_CH or num in seen:
                continue
            seen.add(num)

            ch_url = urljoin(base_url, href)

            # كشف القفل (Naver fast-pass / ic_lock)
            locked = False
            reason = "free"
            if detect_lock:
                parent = a.parent or a
                item = parent
                for _ in range(4):  # 4 مستويات للأعلى
                    if item is None:
                        break
                    el_str = str(item)
                    if any(k in el_str for k in ["ic_lock", "ico-lock", "lk_lock", "lock-icon", "fastpass", "fast-pass"]):
                        locked = True
                        reason = "lock-icon"
                        break
                    if any(k in el_str for k in ["ic_free", "ico-free", "lk_free", "free-episode"]):
                        locked = False
                        reason = "free-icon"
                        break
                    try:
                        item = item.parent
                    except Exception:
                        break

            result[num] = {"url": ch_url, "locked": locked, "reason": reason}

    return result


def _detect_total_pages(html: str) -> int:
    """اكتشاف عدد صفحات الـ pagination."""
    soup  = BeautifulSoup(html, "html.parser")
    pages = set()

    # كل الروابط التي تحتوي page=N
    for a in soup.find_all("a", href=True):
        m = re.search(r"[?&]page=(\d+)", a["href"])
        if m:
            pages.add(int(m.group(1)))

    if pages:
        return max(pages)

    # نمط "1 / 2" في النص
    text = soup.get_text(" ")
    m    = re.search(r"\b(\d+)\s*/\s*(\d+)\b", text)
    if m:
        return int(m.group(2))

    return 1


class NaverProvider(BaseProvider):

    def __init__(self):
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        super().__init__(scraper=scraper)

    def fetch_html(self, url: str, extra_headers: dict = None, timeout: int = 25) -> Optional[str]:
        h = {**HEADERS, **(extra_headers or {})}
        return super().fetch_html(url, extra_headers=h, timeout=timeout)

    def _get(self, url: str) -> str | None:
        """جلب HTML من URL مع headers مناسبة."""
        try:
            return self.fetch_html(url, extra_headers=HEADERS)
        except Exception as e:
            print(f"[Naver] fetch error ({url[-60:]}): {e}")
        return None

    # ── صور الفصل ─────────────────────────────────────────────────────────
    async def get_images(self, url: str) -> list[str]:
        loop = asyncio.get_event_loop()

        def _scrape():
            html = self._get(_to_mobile(url))
            if not html:
                html = self._get(url)
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")
            for sel in [
                ".wt_viewer img", ".viewer_lst img",
                ".toon_view_lst img", ".toon_image",
                "#comic_view_area img", ".content_viewer img",
                ".wrap_viewer img",
            ]:
                imgs = []
                for img in soup.select(sel):
                    src = (img.get("data-src") or img.get("src") or img.get("data-lazy-src") or "").strip()
                    if src.startswith("http") and "pstatic.net" in src:
                        imgs.append(src)
                if imgs:
                    return imgs
            return []

        return await loop.run_in_executor(None, _scrape)

    # ── كل الفصول (رابط فقط) ──────────────────────────────────────────────
    async def get_all_chapters(self, series_url: str) -> dict[float, str]:
        rich = await self._fetch_all_pages(series_url, detect_lock=False)
        return {num: info["url"] for num, info in rich.items()}

    # ── كل الفصول مع حالة القفل ────────────────────────────────────────────
    async def get_chapters_with_lock_info(self, series_url: str) -> dict[float, dict]:
        return await self._fetch_all_pages(series_url, detect_lock=True)

    # ── الدالة الجوهرية ────────────────────────────────────────────────────
    async def _fetch_all_pages(
        self, series_url: str, detect_lock: bool = True
    ) -> dict[float, dict]:
        loop    = asyncio.get_event_loop()
        # نستخدم mobile URL دائماً
        mob_url = _to_mobile(series_url)
        all_chs: dict[float, dict] = {}

        # ── صفحة 1 ────────────────────────────────────────────────────────
        p1_url = _make_page_url(mob_url, 1, asc=True)

        def _fetch_page(p_url: str) -> dict[float, dict]:
            html = self._get(p_url)
            if not html:
                return {}
            return _extract_from_html(html, mob_url, detect_lock)

        def _fetch_page_and_total(p_url: str):
            html = self._get(p_url)
            if not html:
                return {}, 1
            chs   = _extract_from_html(html, mob_url, detect_lock)
            pages = _detect_total_pages(html)
            return chs, pages

        p1_chs, total_pages = await loop.run_in_executor(None, _fetch_page_and_total, p1_url)
        all_chs.update(p1_chs)

        if not p1_chs:
            # جرب بدون تغيير sortOrder
            alt_url = _make_page_url(mob_url, 1, asc=False)
            p1_chs2, total_pages = await loop.run_in_executor(None, _fetch_page_and_total, alt_url)
            all_chs.update(p1_chs2)

        print(f"[Naver] {mob_url} → page1={len(p1_chs)} chs, total_pages={total_pages}")

        if not all_chs:
            return {}

        # ── إذا الـ pagination لم تُكتشف — جرب حتى 20 صفحة ──────────────
        if total_pages <= 1 and len(all_chs) >= 25:
            total_pages = 20   # سيتوقف عند عدم وجود فصول جديدة

        # ── باقي الصفحات بالتوازي ─────────────────────────────────────────
        if total_pages > 1:
            tasks = [
                loop.run_in_executor(
                    None, _fetch_page,
                    _make_page_url(mob_url, p, asc=True)
                )
                for p in range(2, total_pages + 1)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for chs in results:
                if isinstance(chs, dict) and chs:
                    before = len(all_chs)
                    all_chs.update(chs)

        print(f"[Naver] Total chapters: {len(all_chs)}")
        return all_chs

    async def get_latest_chapter(self, series_url: str) -> Optional[float]:
        chs = await self.get_all_chapters(series_url)
        return max(chs.keys()) if chs else None
