"""
paginated_scraper.py — جامع الفصول الذكي مع دعم الصفحات المتعددة

يحل مشاكل:
  1. المواقع التي تقسّم الفصول عبر صفحات متعددة (مثل الصورة المرفقة)
  2. كشف عدد الصفحات تلقائياً بدل التجربة العمياء
  3. جمع حالة القفل (حر/مدفوع) في نفس الجولة
  4. دعم "Episode" مش بس "Chapter" keyword

أنماط مدعومة:
  • ?page=N               (الأكثر شيوعاً)
  • /page/N/
  • ?p=N
  • ?offset=N  (offset = (N-1) * per_page)
  • ?chapter_page=N
  • AJAX/JSON pagination
"""

from __future__ import annotations
import re
import json
import math
from typing import Callable, Optional
from urllib.parse import urlparse, urljoin, urlencode, parse_qs, urlunparse, urlencode
from bs4 import BeautifulSoup
from .lock_detector import detect_lock_from_element, bulk_detect, get_site_rule

import logging
log = logging.getLogger("paginated_scraper")


# ── أنماط استخراج رقم الفصل ──────────────────────────────────────────
_NUM_RE = re.compile(
    r'(?:'
    r'(?:chapter|chap|ch|episode|ep|epi|화|화|話|回|第)\s*[:\-.]?\s*'
    r')'
    r'(\d+(?:[._]\d+)?)',
    re.I
)
_URL_NUM_RE = re.compile(
    r'(?:/|-)(?:chapter|chap|ch|episode|ep|ep-)[-_]?(\d+(?:\.\d+)?)'
    r'|/(\d+)/?(?:\?|$)',
    re.I
)


def _extract_num(text: str, href: str = "") -> Optional[float]:
    """استخراج رقم الفصل من نص أو URL."""
    m = _NUM_RE.search(text)
    if m:
        try:
            return float(m.group(1).replace("_", "."))
        except Exception:
            pass
    if href:
        m = _URL_NUM_RE.search(href)
        if m:
            try:
                return float((m.group(1) or m.group(2)).replace("_", "."))
            except Exception:
                pass
    return None


# ── كشف إجمالي الصفحات ────────────────────────────────────────────────
def detect_total_pages(soup: BeautifulSoup, base_url: str) -> int:
    """
    يقرأ إجمالي عدد صفحات الـ pagination من HTML.
    يجرب أنماط متعددة ويرجع 1 إذا لم يجد.
    """
    # 1. pagination links — ابحث عن أعلى رقم في روابط التنقل
    for sel in [
        ".pagination a", ".paginat a", ".pages a",
        "[class*='page'] a", ".page-numbers a",
        ".wp-pagenavi a", ".nav-links a",
        "ul.pagination li a", ".c-paginate a",
    ]:
        links = soup.select(sel)
        nums  = []
        for a in links:
            t = a.get_text(strip=True)
            if t.isdigit():
                nums.append(int(t))
        if nums:
            return max(nums)

    # 2. total_pages في JSON
    for script in soup.find_all("script"):
        text = script.string or ""
        m = re.search(r'"(?:total_?pages?|totalPage|pageCount|lastPage)"\s*:\s*(\d+)', text)
        if m:
            return int(m.group(1))

    # 3. "الصفحة X من Y" أو "Page X of Y"
    body = soup.get_text(" ")
    m = re.search(r'(?:page|صفحة)\s+\d+\s+(?:of|من)\s+(\d+)', body, re.I)
    if m:
        return int(m.group(1))

    # 4. next/last button مع رقم في href
    for a in soup.find_all("a", href=True):
        cls = " ".join(a.get("class", []))
        if re.search(r'last|آخر|last-?page', cls, re.I):
            href = a["href"]
            m = re.search(r'[?&]page=(\d+)', href)
            if m:
                return int(m.group(1))

    return 1   # لم يُعثر على pagination


# ── قائمة الـ chapter selectors ──────────────────────────────────────
CHAPTER_SELECTORS = [
    # Madara WordPress
    "li.wp-manga-chapter",
    ".chapters-list li",
    # شائع
    ".chapter-list li",
    ".chapter-list-item",
    "ul.row-content-chapter li",
    # Tapas / Webtoon style
    ".episode-list .item",
    ".episode-list li",
    ".episode_list li",
    "[class*='episode-item']",
    # Table rows
    "tr.chapter", "tbody tr",
    # Generic
    "li[class*='chapter']",
    "li[class*='episode']",
    ".chapter-row",
    "[data-chapter]",
    "[data-episode]",
]

# ── استخراج الفصول من soup ───────────────────────────────────────────
def extract_chapters_from_soup(
    soup: BeautifulSoup,
    base_url: str,
    detect_lock: bool = True,
) -> dict[float, dict]:
    """
    يستخرج الفصول من soup صفحة واحدة.
    يرجع:
        { chapter_num: {"url": str, "locked": bool, "reason": str} }
    """
    parsed = urlparse(base_url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    site_rule = get_site_rule(base_url)
    result: dict[float, dict] = {}

    # اختر selectors حسب الموقع أو الافتراضي
    selectors = (
        [site_rule["episode_sel"]] if site_rule.get("episode_sel")
        else CHAPTER_SELECTORS
    )
    link_sel  = site_rule.get("link_sel", "a")
    num_attr  = site_rule.get("num_attr")
    num_re    = site_rule.get("num_from_url")

    for sel in selectors:
        items = soup.select(sel)
        if not items:
            continue

        for item in items:
            # رابط
            if link_sel == "self":
                a = item
            else:
                a = item.select_one(link_sel) if link_sel != "a" else item.find("a", href=True)
                if not a:
                    a = item.find("a", href=True)
            if not a:
                continue
            href = a.get("href", "").strip()
            if not href:
                continue
            if not href.startswith("http"):
                href = urljoin(domain, href)
            # فلتر: لا تعليقات، لا تسجيل دخول
            if any(x in href for x in ["#comment", "/login", "/register", "/signup"]):
                continue

            # رقم الفصل
            num = None
            if num_attr and item.get(num_attr):
                try:
                    num = float(item[num_attr])
                except Exception:
                    pass
            if num is None and num_re:
                m = re.search(num_re, href)
                if m:
                    try:
                        num = float(m.group(1))
                    except Exception:
                        pass
            if num is None:
                text = item.get_text(" ", strip=True)
                num  = _extract_num(text, href)
            if num is None:
                continue

            # حالة القفل
            lock_info = {"locked": False, "reason": "default"}
            if detect_lock:
                lock_sel = site_rule.get("lock_selector")
                free_sel = site_rule.get("free_selector")
                if lock_sel and item.select_one(lock_sel):
                    lock_info = {"locked": True, "reason": f"site:{lock_sel}"}
                elif free_sel and item.select_one(free_sel):
                    lock_info = {"locked": False, "reason": "site:free"}
                else:
                    lock_info = detect_lock_from_element(item)

            result[num] = {"url": href, **lock_info}

        if result:
            break   # أول selector ينجح يكفي

    return result


# ── Pagination patterns ───────────────────────────────────────────────
def _pagination_urls(base_url: str, page_num: int) -> list[str]:
    """يرجع قائمة بروابط الصفحة page_num لأنماط مختلفة."""
    u      = base_url.rstrip("/")
    parsed = urlparse(base_url)
    qs     = parse_qs(parsed.query)

    urls = []

    # ?page=N
    qs2 = {**qs, "page": [str(page_num)]}
    p   = parsed._replace(query=urlencode({k: v[0] for k, v in qs2.items()}))
    urls.append(urlunparse(p))

    # /page/N/
    path_clean = re.sub(r'/page/\d+/?$', '', parsed.path).rstrip('/')
    urls.append(urlunparse(parsed._replace(
        path=f"{path_clean}/page/{page_num}/", query=""
    )))

    # ?p=N
    qs3 = {**qs, "p": [str(page_num)]}
    p   = parsed._replace(query=urlencode({k: v[0] for k, v in qs3.items()}))
    urls.append(urlunparse(p))

    # ?chapter_page=N
    qs4 = {**qs, "chapter_page": [str(page_num)]}
    p   = parsed._replace(query=urlencode({k: v[0] for k, v in qs4.items()}))
    urls.append(urlunparse(p))

    return urls


# ── الجامع الرئيسي ───────────────────────────────────────────────────
class PaginatedScraper:
    """
    جامع الفصول الذكي — يدعم:
      • Auto-detection للـ pagination
      • كشف القفل لكل فصل
      • أنماط URL متعددة

    الاستخدام:
        scraper = PaginatedScraper(fetch_fn)
        chapters = await scraper.get_all_chapters(url)
        # chapters = { num: {"url":..., "locked":..., "reason":...} }
    """

    def __init__(self, fetch_fn: Callable[[str], str | None], max_pages: int = 50):
        """
        fetch_fn: async أو sync function تأخذ URL وترجع HTML أو None.
        """
        self._fetch  = fetch_fn
        self.max_pages = max_pages

    async def get_all_chapters(
        self,
        url: str,
        detect_lock: bool = True,
    ) -> dict[float, dict]:
        """
        يجمع كل الفصول من جميع الصفحات.
        يرجع { num: {"url": str, "locked": bool, "reason": str} }
        """
        import asyncio, inspect

        async def _do_fetch(u):
            if inspect.iscoroutinefunction(self._fetch):
                return await self._fetch(u)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._fetch, u)

        all_chapters: dict[float, dict] = {}
        last_pattern_worked = None
        total_pages         = 1

        # ── صفحة 1: الرئيسية ─────────────────────────────────────────
        html = await _do_fetch(url)
        if not html:
            return {}

        soup = BeautifulSoup(html, "html.parser")
        chs  = extract_chapters_from_soup(soup, url, detect_lock)
        all_chapters.update(chs)

        # كم عدد الصفحات؟
        total_pages = detect_total_pages(soup, url)
        log.debug(f"[Paginated] {url} → {total_pages} pages detected, "
                  f"page1 has {len(chs)} chapters")

        if total_pages <= 1:
            return all_chapters

        # ── صفحات 2 → N بالتوازي ─────────────────────────────────────
        # نجرب كل نمط URL للصفحة 2 أولاً لنكتشف أيها يعمل
        working_pattern_fn = None
        for pattern_fn in [
            lambda n: f"{url.rstrip('/')}?page={n}",
            lambda n: re.sub(r'/page/\d+/?$', '', url).rstrip('/') + f"/page/{n}/",
            lambda n: f"{url.rstrip('/')}?p={n}",
            lambda n: f"{url.rstrip('/')}?chapter_page={n}",
        ]:
            test_url = pattern_fn(2)
            if test_url == url:
                continue
            test_html = await _do_fetch(test_url)
            if not test_html or len(test_html) < 500:
                continue
            test_soup = BeautifulSoup(test_html, "html.parser")
            test_chs  = extract_chapters_from_soup(test_soup, url, detect_lock)
            if test_chs:
                # نمط ناجح!
                all_chapters.update(test_chs)
                working_pattern_fn = pattern_fn
                log.debug(f"[Paginated] Pattern works: page2 → {len(test_chs)} chapters")
                break

        if not working_pattern_fn:
            return all_chapters

        # ── الصفحات الباقية بالتوازي ─────────────────────────────────
        # max 10 طلبات بالتوازي
        BATCH = 10
        remaining = list(range(3, total_pages + 1))

        for i in range(0, len(remaining), BATCH):
            batch = remaining[i:i + BATCH]
            tasks = [_do_fetch(working_pattern_fn(n)) for n in batch]
            htmls = await asyncio.gather(*tasks, return_exceptions=True)

            for page_n, page_html in zip(batch, htmls):
                if isinstance(page_html, Exception) or not page_html:
                    continue
                page_soup = BeautifulSoup(page_html, "html.parser")
                page_chs  = extract_chapters_from_soup(page_soup, url, detect_lock)
                if not page_chs:
                    # لا فصول = نهاية القائمة
                    log.debug(f"[Paginated] Page {page_n}: empty → stopping")
                    break
                before = len(all_chapters)
                all_chapters.update(page_chs)
                if len(all_chapters) == before:
                    # نفس الفصول = آخر صفحة حقيقية
                    log.debug(f"[Paginated] Page {page_n}: duplicate → stopping")
                    break
                log.debug(f"[Paginated] Page {page_n}: +{len(page_chs)} chapters")

        log.debug(f"[Paginated] Total: {len(all_chapters)} chapters from {url}")
        return all_chapters


# ── استخراج الـ chapters من AJAX/JSON ────────────────────────────────
def extract_chapters_from_json(data, base_url: str) -> dict[float, dict]:
    """
    يستخرج الفصول من JSON response.
    يدعم هياكل متعددة.
    """
    result = {}
    text   = json.dumps(data) if not isinstance(data, str) else data
    parsed = urlparse(base_url)
    domain = f"{parsed.scheme}://{parsed.netloc}"

    # نمط رابط الفصل مع رقم
    for m in re.finditer(
        r'"(?:url|href|link|chapter_url)"\s*:\s*"([^"]+chapter[s]?[-/](\d+(?:\.\d+)?)[^"]*)"',
        text, re.I
    ):
        href = m.group(1).replace('\\/', '/').replace('\\u002F', '/')
        if not href.startswith("http"):
            href = urljoin(domain, href)
        try:
            num = float(m.group(2))
            result[num] = {"url": href, "locked": False, "reason": "json"}
        except Exception:
            pass

    return result
