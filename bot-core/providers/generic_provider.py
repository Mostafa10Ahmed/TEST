"""
generic_provider.py — المزود العام المحسّن v2

مستوحى من scraper.js v5 — تحسينات رئيسية:
  1. Two-Pool Image Collection:
     - Pool A: URLs مؤكدة بالامتداد (.jpg/.png/.webp/.gif)
     - Pool B: URLs CDN بدون امتداد (مستخرجة من script tags كـ arrays)
     → لا نحذف CDN URLs لمجرد غياب الامتداد!
  2. JS Source Scan: يقرأ arrays الصور من داخل <script> مثلاً:
       var images = ["https://cdn.../001", ...]
       window.__DATA__ = { pages: [...] }
  3. Pagination ذكي: يقرأ عدد الصفحات من HTML، لا يجرب عشوائياً
  4. Cookie support في headers التحميل
"""

from __future__ import annotations
import re
import json
from bs4 import BeautifulSoup
from .base_provider import BaseProvider
from urllib.parse import urljoin, urlparse
from typing import List, Optional


# ── Regex: URL مؤكد كصورة بامتداد ─────────────────────────────────────
_EXT_RE = re.compile(
    r'https?://[^\s"\'<>]+?\.(?:jpe?g|png|webp|gif)(?:\?[^\s"\'<>]*)?',
    re.I
)

# ── Regex: أي https URL داخل quotes (للـ JS scan) ────────────────────
_JS_URL_RE = re.compile(
    r'["\x60](https?://[^\s"\'<>\x60]{10,})["\x60]',
    re.I
)

# ── أنماط JS arrays: var x = [...] / window.x = [...] ────────────────
_JS_ARRAY_RE = re.compile(
    r'(?:var\s+\w+\s*=\s*|window\.\w+\s*=\s*|\w+\s*:\s*)\[([^\]]{30,})\]',
    re.I
)

# ── noise: أشياء يجب استبعادها من URLs الصور ─────────────────────────
_NOISE = re.compile(
    r'logo|avatar|icon|banner|\bads\b|thumb|button|sprite|'
    r'pixel|tracking|google|facebook|twitter|gravatar',
    re.I
)

# ── الـ selectors الشائعة لقارئات المانجا ────────────────────────────
READER_SELECTORS = [
    "#readerarea",          # Madara
    ".rdminimal",
    ".reading-content",
    ".chapter-content",
    ".chapter-images",
    "#chapter-content",
    ".viewer-container",
    ".manga-reader",
    "[class*='reader']",
    "[id*='reader']",
    "[class*='chapter']",
    ".page-break",
    ".entry-content",
    ".comic-reader",
    ".webtoon-viewer",
]

# ── selectors للصور داخل القارئ ──────────────────────────────────────
IMG_SELECTORS = [
    "img[data-src]", "img[data-lazy-src]", "img[data-original]",
    "img[data-cfsrc]", "img[data-url]", "img[src]",
    "img.wp-manga-chapter-img", "img.chapter-img",
]


class GenericProvider(BaseProvider):
    """مزود عام ذكي: يجرب HTML + Next.js + Pagination + API — v2"""

    # ═══════════════════════════════════════════════════════════════
    #  get_all_chapters — بالـ pagination الذكي
    # ═══════════════════════════════════════════════════════════════
    def get_all_chapters(self, url: str) -> dict:
        html = self.fetch_html(url)
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')

        # 1. __NEXT_DATA__
        nd = soup.find('script', id='__NEXT_DATA__')
        if nd:
            try:
                chs = self._chapters_from_json(json.dumps(json.loads(nd.string)), url)
                if len(chs) > 5:
                    return chs
            except Exception:
                pass

        # 2. HTML links
        chs = self._from_html_links(soup, url)

        # 3. Pagination ذكي إذا الفصول أقل من 25
        if len(chs) < 25:
            extra = self._smart_paginate(url, soup, html)
            chs.update(extra)

        # 4. fallback: APIs عامة
        if not chs:
            chs = self._try_generic_apis(url)

        return chs

    def get_latest_chapter(self, url: str) -> Optional[float]:
        chs = self.get_all_chapters(url)
        return max(chs.keys()) if chs else None

    # ═══════════════════════════════════════════════════════════════
    #  get_images — Two-Pool + JS Scan
    # ═══════════════════════════════════════════════════════════════
    def get_images(self, url: str) -> List[str]:
        html = self.fetch_html(url)
        if not html:
            return []

        soup = BeautifulSoup(html, 'html.parser')

        # Pool A: confirmed by extension (ترتيب عالٍ)
        pool_ext: list[str] = []
        # Pool B: CDN URLs من JS بدون امتداد (ترتيب عالٍ أيضاً بعد التحقق)
        pool_cdn: list[str] = []

        # ── 1. __NEXT_DATA__ ─────────────────────────────────────────
        nd = soup.find('script', id='__NEXT_DATA__')
        if nd:
            try:
                found = self._extract_images_two_pool(
                    json.dumps(json.loads(nd.string))
                )
                if found:
                    return found
            except Exception:
                pass

        # ── 2. DOM selectors — قارئ المانجا ─────────────────────────
        for sel in READER_SELECTORS:
            container = soup.select_one(sel)
            if not container:
                continue
            dom_imgs = self._collect_dom_images(container)
            if dom_imgs:
                return dom_imgs

        # ── 3. JS Source Scan — الخطوة الجديدة من scraper.js ─────────
        js_images = self._js_source_scan(soup)
        if js_images:
            return js_images

        # ── 4. كل الصور في الصفحة ────────────────────────────────────
        all_imgs = self._collect_dom_images(soup)
        if all_imgs:
            return all_imgs

        # ── 5. Regex على HTML الخام ──────────────────────────────────
        return self._extract_images_two_pool(html)

    # ═══════════════════════════════════════════════════════════════
    #  Two-Pool Image Extraction
    # ═══════════════════════════════════════════════════════════════
    def _extract_images_two_pool(self, text: str) -> list[str]:
        """
        Pool A: URLs بامتداد صورة مؤكد
        Pool B: URLs CDN/token بدون امتداد — مستخرجة من arrays

        المنطق: لا نُعيد فلترة Pool A بعد جمعه
        (مثل scraper.js: confirmedByType لا تُعاد فلترته)
        """
        seen: set[str] = set()
        pool_ext: list[str] = []   # Pool A
        pool_cdn: list[str] = []   # Pool B

        # Pool A: امتداد صورة
        for m in _EXT_RE.finditer(text):
            src = _clean_url(m.group(0))
            if src and src not in seen and not _NOISE.search(src):
                seen.add(src)
                pool_ext.append(src)

        # Pool B: arrays من JS
        for arr_m in _JS_ARRAY_RE.finditer(text):
            arr_content = arr_m.group(1)
            for url_m in _JS_URL_RE.finditer(arr_content):
                src = _clean_url(url_m.group(1))
                if (src and src not in seen
                        and not _NOISE.search(src)
                        and _looks_like_image_url(src)):
                    seen.add(src)
                    pool_cdn.append(src)

        # دمج: pool_ext أولاً (أكثر دقة)، ثم pool_cdn
        combined = pool_ext + pool_cdn

        # فلترة: حذف logos وإيقونات صغيرة وسواها
        return _deduplicate_and_filter(combined)

    def _collect_dom_images(self, container) -> list[str]:
        """يجمع صور المانجا من عنصر HTML."""
        seen   = set()
        images = []

        for img in container.find_all('img'):
            src = (
                img.get('data-src') or
                img.get('data-lazy-src') or
                img.get('data-original') or
                img.get('data-cfsrc') or
                img.get('data-url') or
                img.get('data-lazy-load') or
                img.get('data-actual-src') or
                img.get('data-imagesource') or
                img.get('data-echo') or
                img.get('src') or
                img.get('data-pagespeed-lazy-src') or
                ''
            ).strip()

            if not src.startswith('http') or src in seen:
                continue

            # تجاهل الصور الصغيرة (أيقونات)
            h = img.get('height', '')
            w = img.get('width', '')
            try:
                if int(str(h).replace('px', '')) < 100:
                    continue
            except Exception:
                pass
            try:
                if int(str(w).replace('px', '')) < 100:
                    continue
            except Exception:
                pass

            if not _NOISE.search(src):
                seen.add(src)
                images.append(src)

        return images

    def _js_source_scan(self, soup: BeautifulSoup) -> list[str]:
        """
        JS Source Scanner — مستوحى من scraper.js v5.

        يفحص كل <script> inline ويستخرج:
         • image URLs من arrays (var images = [...])
         • window.__DATA__, window.CHAPTER_INFO, window.images, window.pics
         • أي array يحوي 3+ URLs تبدو كصور
        """
        seen   = set()
        found  = []

        for script in soup.find_all('script'):
            content = script.string
            if not content:
                continue

            # فحص هل يحتوي على صور قبل المعالجة الثقيلة
            if not any(x in content for x in [
                'http', '.webp', '.jpg', '.jpeg', '.png', 'images', 'pages', 'pics'
            ]):
                continue

            # اسحب كل arrays
            for arr_m in _JS_ARRAY_RE.finditer(content):
                arr_content = arr_m.group(1)
                urls_in_arr = _JS_URL_RE.findall(arr_content)
                if len(urls_in_arr) < 2:
                    continue   # array صغيرة — ليست فصلاً

                for src in urls_in_arr:
                    src = _clean_url(src)
                    if (src and src not in seen
                            and not _NOISE.search(src)
                            and _looks_like_image_url(src)):
                        seen.add(src)
                        found.append(src)

            # خاص: window.__DATA__ / window.CHAPTER_INFO / window.images
            for var_name in ['__DATA__', 'CHAPTER_INFO', 'images', 'pics', 'pages',
                              'pageUrls', 'imageUrls', 'chapterImages']:
                pattern = re.compile(
                    rf'(?:window\.{re.escape(var_name)}\s*=\s*|'
                    rf'"{re.escape(var_name)}"\s*:\s*)(\[.*?\]|\{{.*?\}})',
                    re.S
                )
                for m in pattern.finditer(content):
                    block = m.group(1)
                    for url_m in _JS_URL_RE.finditer(block):
                        src = _clean_url(url_m.group(1))
                        if (src and src not in seen
                                and not _NOISE.search(src)
                                and _looks_like_image_url(src)):
                            seen.add(src)
                            found.append(src)

        return _deduplicate_and_filter(found)

    # ═══════════════════════════════════════════════════════════════
    #  Pagination ذكي
    # ═══════════════════════════════════════════════════════════════
    def _smart_paginate(
        self,
        base_url: str,
        soup: BeautifulSoup,
        first_html: str,
    ) -> dict:
        """
        Pagination ذكي:
        1. يقرأ إجمالي الصفحات من HTML (لا يجرب عمياً)
        2. يجرب أنماط URL متعددة
        3. يتوقف عند أول نمط ناجح
        """
        from .paginated_scraper import detect_total_pages
        total_pages = detect_total_pages(soup, base_url)

        if total_pages <= 1:
            # جرب الـ pagination القديمة كـ fallback
            return self._paginate_chapters(
                base_url,
                lambda h, u: self._from_html_links(BeautifulSoup(h, 'html.parser'), u),
                max_pages=15,
            )

        all_chs: dict = {}
        patterns = [
            lambda u, n: f"{u.rstrip('/')}?page={n}",
            lambda u, n: re.sub(r'/page/\d+/?$', '', u).rstrip('/') + f"/page/{n}/",
            lambda u, n: f"{u.rstrip('/')}?p={n}",
        ]
        working = None

        # اكتشف النمط الصحيح من صفحة 2
        for pat in patterns:
            pg2_url  = pat(base_url, 2)
            pg2_html = self.fetch_html(pg2_url)
            if not pg2_html or len(pg2_html) < 500:
                continue
            pg2_soup = BeautifulSoup(pg2_html, 'html.parser')
            pg2_chs  = self._from_html_links(pg2_soup, base_url)
            if pg2_chs:
                all_chs.update(pg2_chs)
                working = pat
                break

        if not working:
            return all_chs

        for n in range(3, min(total_pages + 1, 41)):
            pg_html = self.fetch_html(working(base_url, n))
            if not pg_html or len(pg_html) < 500:
                break
            pg_chs = self._from_html_links(BeautifulSoup(pg_html, 'html.parser'), base_url)
            if not pg_chs:
                break
            before = len(all_chs)
            all_chs.update(pg_chs)
            if len(all_chs) == before:
                break

        return all_chs

    # ═══════════════════════════════════════════════════════════════
    #  helpers
    # ═══════════════════════════════════════════════════════════════
    def _from_html_links(self, soup, base_url: str) -> dict:
        chs    = {}
        parsed = urlparse(base_url)
        base   = f"{parsed.scheme}://{parsed.netloc}"

        for a in soup.find_all('a', href=True):
            href = a['href']
            if not href.startswith('http'):
                href = urljoin(base, href)
            text = a.get_text(strip=True)
            val  = self.extract_chapter_number(text)
            if val is None:
                m = re.search(r'(?:chapter|chap|ch|episode|ep)[s]?[-/](\d+(?:\.\d+)?)', href, re.I)
                if m:
                    try:
                        val = float(m.group(1))
                    except Exception:
                        pass
            if val is not None and base.split('//')[1].split('/')[0] in href:
                chs[val] = href
        return chs

    def _chapters_from_json(self, text: str, base_url: str) -> dict:
        chs    = {}
        parsed = urlparse(base_url)
        base   = f"{parsed.scheme}://{parsed.netloc}"
        for m in re.finditer(
            r'"((?:https?://[^"]+|/[^"]+)/(?:chapter|episode)[s]?[-/](\d+(?:\.\d+)?)(?:[/"\\]))',
            text,
        ):
            href = m.group(1).replace('\\u002F', '/').replace('\\', '')
            if not href.startswith('http'):
                href = urljoin(base, href)
            try:
                chs[float(m.group(2))] = href
            except Exception:
                pass
        return chs

    def _try_generic_apis(self, series_url: str) -> dict:
        parsed = urlparse(series_url)
        base   = f"{parsed.scheme}://{parsed.netloc}"
        slug   = series_url.rstrip('/').split('/')[-1]
        chs    = {}
        for ep in [
            f"{base}/api/chapters?series={slug}&limit=9999",
            f"{base}/api/manga/{slug}/chapters",
            f"{base}/api/comic/{slug}/chapters",
        ]:
            try:
                data = self.fetch_json(ep)
                if data:
                    found = self._chapters_from_json(json.dumps(data), series_url)
                    if found:
                        chs.update(found)
                        break
            except Exception:
                continue
        return chs


# ── دوال مساعدة ──────────────────────────────────────────────────────
def _clean_url(src: str) -> str:
    return (src.replace('\\u002F', '/').replace('\\/', '/')
               .replace('\\', '').strip().rstrip('"\''))


def _looks_like_image_url(url: str) -> bool:
    """
    هل يبدو الـ URL مثل صورة؟
    يقبل:
     - URL بامتداد صورة
     - URL CDN مع path يشبه صور المانجا (page_001, /img/, /images/, /manga/)
     - URL يحوي token/hash (CDN authenticated)
    """
    low = url.lower()
    # امتداد صريح
    if re.search(r'\.(jpe?g|png|webp|gif)(\?|$)', low):
        return True
    # مسار CDN شائع
    if any(x in low for x in ['/img/', '/images/', '/manga/', '/chapter/',
                                '/page/', 'cdn.', 'media.', 'static.']):
        return True
    # URL مع hash/token طويل (CDN authenticated)
    path = urlparse(url).path
    if len(path) > 20 and not path.endswith('/'):
        return True
    return False


def _deduplicate_and_filter(images: list[str]) -> list[str]:
    """إزالة المكررات والضوضاء — مع الحفاظ على الترتيب."""
    seen   = set()
    result = []
    for src in images:
        if not src or src in seen:
            continue
        seen.add(src)
        # حذف صور صغيرة جداً / noise
        if _NOISE.search(src):
            continue
        result.append(src)
    return result


# ── حفاظ على التوافق مع الكود القديم ────────────────────────────────
def urlparse(url):
    from urllib.parse import urlparse as _up
    return _up(url)
