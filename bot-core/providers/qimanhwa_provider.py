import asyncio
import json
import re
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

from .base_provider import BaseProvider


class QimanhwaProvider(BaseProvider):
    """
    مزود Qimanhwa (Qi Manhwa) - Angular-based SPA

    الاستراتيجية:
    1. استدعاء REST API مباشر (Angular يستخدم API endpoints)
    2. تحليل SSR JSON إن وجد في الـ HTML
    3. إرجاع قائمة فارغة ← يُفعّل Playwright fallback في ProviderManager
    """

    def __init__(self):
        super().__init__()
        self.base_url = "https://qimanhwa.com"
        self.headers["Referer"] = self.base_url + "/"

    @staticmethod
    def _extract_slug(url: str) -> str:
        """Extract series slug: /series/{slug} or /series/{slug}/chapter/N"""
        m = re.search(r"/series/([^/?#]+)", url)
        return m.group(1) if m else ""

    # ── get_images ────────────────────────────────────────────────────────────
    async def get_images(self, url: str) -> list:
        """
        Fetch chapter page images.
        Returns empty list if no real chapter images found → triggers Playwright fallback.
        """
        try:
            # Layer 1: REST API
            images = await self._api_get_images(url)
            if images:
                return images

            # Layer 2: HTML scraping (SSR data or reader selectors only)
            html = self.fetch_html(url, {"Referer": self.base_url + "/"})
            if html:
                images = self._extract_from_html(html, url)
                if images:
                    return images

            # Return empty → triggers Playwright fallback in ProviderManager
            print(
                f"[Qimanhwa] No images via static scraping → Playwright fallback: {url}"
            )
            return []
        except Exception as e:
            print(f"[Qimanhwa] get_images error: {e}")
            return []

    async def _api_get_images(self, chapter_url: str) -> list:
        """Try common Angular REST API patterns for chapter images."""
        parsed = urlparse(chapter_url)
        base_api = f"{parsed.scheme}://{parsed.netloc}"

        m = re.search(r"/series/([^/]+)/chapter[s]?/([^/?#]+)", chapter_url)
        if not m:
            return []

        slug = m.group(1)
        chapter_num = m.group(2)

        candidates = [
            f"{base_api}/api/chapters/{slug}/{chapter_num}",
            f"{base_api}/api/series/{slug}/chapters/{chapter_num}",
            f"{base_api}/api/v1/chapters/{slug}/{chapter_num}",
            f"{base_api}/api/series/{slug}/chapter/{chapter_num}/pages",
            f"{base_api}/api/comic/{slug}/chapter/{chapter_num}",
        ]

        extra_headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": chapter_url,
            "X-Requested-With": "XMLHttpRequest",
        }

        for api_url in candidates:
            try:
                data = self.fetch_json(api_url, extra_headers=extra_headers)
                if not data:
                    continue
                images = self._extract_images_from_api(data)
                if images:
                    print(f"[Qimanhwa] API images OK: {api_url} → {len(images)}")
                    return images
            except Exception:
                continue

        return []

    def _extract_images_from_api(self, data) -> list:
        """Extract image URLs from API JSON response (various shapes)."""
        images = []

        if isinstance(data, list):
            for item in data:
                if isinstance(item, str) and item.startswith("http"):
                    images.append(item)
                elif isinstance(item, dict):
                    src = (
                        item.get("url")
                        or item.get("image")
                        or item.get("src")
                        or item.get("page_image")
                        or ""
                    )
                    if src and src.startswith("http"):
                        images.append(src)
        elif isinstance(data, dict):
            for key in ("images", "pages", "chapter_images", "data", "results"):
                val = data.get(key)
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, str) and item.startswith("http"):
                            images.append(item)
                        elif isinstance(item, dict):
                            src = (
                                item.get("url")
                                or item.get("image")
                                or item.get("src")
                                or ""
                            )
                            if src and src.startswith("http"):
                                images.append(src)
                    if images:
                        break

        return [
            img
            for img in images
            if not any(x in img.lower() for x in ["logo", "icon", "avatar", "banner"])
        ]

    def _extract_from_html(self, html: str, url: str) -> list:
        """
        Extract ONLY real chapter images from HTML.
        Does NOT return generic page images — that would block Playwright fallback.
        """
        soup = BeautifulSoup(html, "html.parser")
        images = []

        # First: look for image arrays in inline JS (SSR/embedded data)
        for script in soup.find_all("script"):
            content = script.string or ""
            if not content:
                continue
            for pattern in [
                r'"(?:images|pages|chapter_images)"\s*:\s*\[([^\]]{20,})\]',
                r"(?:images|pages)\s*=\s*\[([^\]]{20,})\]",
            ]:
                m = re.search(pattern, content, re.S)
                if m:
                    for url_m in re.finditer(
                        r'["\'](https?://[^"\']+\.(?:jpg|jpeg|webp|png)[^"\']*)["\']',
                        m.group(1),
                    ):
                        src = url_m.group(1)
                        if src not in images:
                            images.append(src)
                    if images:
                        return images

        # Second: reader container selectors only (not generic img tags)
        for sel in [
            ".reading-content img",
            "#readerarea img",
            ".chapter-content img",
            ".chapter-images img",
            "[class*='reader'] img",
            "[class*='chapter'] img",
        ]:
            for img in soup.select(sel):
                src = (
                    img.get("src")
                    or img.get("data-src")
                    or img.get("data-lazy-src")
                    or ""
                ).strip()
                if not src.startswith("http"):
                    continue
                if any(
                    x in src.lower()
                    for x in ["logo", "icon", "avatar", "discord", "banner"]
                ):
                    continue
                if src not in images:
                    images.append(src)
            if images:
                return images

        # Intentionally NOT returning generic img tags — return [] to trigger Playwright
        return []

    # ── get_all_chapters ──────────────────────────────────────────────────────
    async def get_all_chapters(self, series_url: str) -> dict:
        """
        Fetch chapter list. Returns empty dict on failure → triggers Playwright fallback.
        """
        try:
            slug = self._extract_slug(series_url)
            if not slug:
                return {}

            # Layer 1: REST API
            chapters = await self._api_get_chapters(series_url, slug)
            if chapters:
                # ترتيب تصاعدي
                return dict(sorted(chapters.items()))

            # Layer 2: HTML (works if site has SSR)
            html = self.fetch_html(series_url)
            if html:
                # Try to find JSON data in script tags first
                chapters = self._extract_from_script_json(html, series_url)
                if chapters:
                    return dict(sorted(chapters.items()))

                chapters = self._chapters_from_html(html, series_url)
                if chapters:
                    return dict(sorted(chapters.items()))

            print(f"[Qimanhwa] No chapters via static scraping for: {series_url}")
            return {}
        except Exception as e:
            print(f"[Qimanhwa] get_all_chapters error: {e}")
            return {}

    async def _api_get_chapters(self, series_url: str, slug: str) -> dict:
        """Try REST API patterns for chapter list with pagination."""
        lock_info = await self.get_chapters_with_lock_info(series_url)
        if lock_info:
            return {num: info["url"] for num, info in lock_info.items()}
        return {}

    def _chapters_from_api(self, data, series_url: str) -> dict:
        """Extract chapters dict from API JSON response."""
        chapters = {}
        parsed = urlparse(series_url)
        slug = self._extract_slug(series_url)
        base = f"{parsed.scheme}://{parsed.netloc}"

        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for key in ("chapters", "data", "results", "items"):
                val = data.get(key)
                if isinstance(val, list):
                    items = val
                    break

        for item in items:
            if not isinstance(item, dict):
                continue
            num = (
                item.get("chapter")
                or item.get("chapter_number")
                or item.get("number")
                or item.get("num")
            )
            href = item.get("url") or item.get("link") or item.get("href") or ""
            ch_slug = item.get("slug")

            if not href and slug and num is not None:
                if ch_slug:
                    href = f"{base}/series/{slug}/{ch_slug}"
                else:
                    href = f"{base}/series/{slug}/chapter/{num}"

            if num is not None and href:
                try:
                    chapters[float(num)] = href
                except Exception:
                    pass

        return chapters

    def _extract_from_script_json(self, html: str, series_url: str) -> dict:
        """Extract chapters from Angular/TransferState JSON in script tags."""
        chapters = {}
        parsed = urlparse(series_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        slug = self._extract_slug(series_url)

        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script"):
            content = script.string or ""
            if '"chapters"' in content or '"slug"' in content:
                # Try to find JSON-like objects
                try:
                    # Look for things that look like chapter arrays or objects
                    # This is a bit broad, but Qimanhwa uses a specific structure
                    # We can try to find all JSON-like blocks
                    matches = re.findall(r"\{.*\}", content)
                    for m in matches:
                        try:
                            data = json.loads(m)

                            # Traverse the dict to find chapters
                            def find_chapters(d):
                                if isinstance(d, dict):
                                    if "chapters" in d and isinstance(
                                        d["chapters"], list
                                    ):
                                        for ch in d["chapters"]:
                                            num = ch.get("chapter_number") or ch.get(
                                                "number"
                                            )
                                            ch_slug = ch.get("slug")
                                            if num is not None:
                                                if ch_slug:
                                                    chapters[float(num)] = (
                                                        f"{base}/series/{slug}/{ch_slug}"
                                                    )
                                                else:
                                                    chapters[float(num)] = (
                                                        f"{base}/series/{slug}/chapter/{num}"
                                                    )
                                    for v in d.values():
                                        find_chapters(v)
                                elif isinstance(d, list):
                                    for item in d:
                                        find_chapters(item)

                            find_chapters(data)
                        except Exception:
                            continue
                except Exception:
                    continue
        return chapters

    def _chapters_from_html(self, html: str, series_url: str) -> dict:
        """Extract chapters from HTML (SSR or partial rendering)."""
        soup = BeautifulSoup(html, "html.parser")
        chapters = {}
        parsed = urlparse(series_url)
        base = f"{parsed.scheme}://{parsed.netloc}"

        # JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") == "ComicIssue":
                        num_str = item.get("issueNumber", "")
                        link = item.get("url", "")
                        if num_str and link:
                            chapters[float(num_str)] = link
            except Exception:
                pass

        if chapters:
            return dict(sorted(chapters.items()))

        # Anchor links
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("http"):
                href = urljoin(base, href)
            m = re.search(r"chapter[-/](\d+(?:\.\d+)?)", href, re.I)
            if m and "qimanhwa" in href:
                num = float(m.group(1))
                if num not in chapters:
                    chapters[num] = href

        # ترتيب تصاعدي (1, 2, 3... بدلاً من الترتيب العشوائي)
        return dict(sorted(chapters.items()))

    async def get_chapters_with_lock_info(self, url: str) -> dict:
        """
        جلب قائمة الفصول مع كشف الفصول المقفولة والمفتوحة ودعم تعدد الصفحات (Pagination).
        """
        try:
            slug = self._extract_slug(url)
            if not slug:
                return {}

            parsed = urlparse(url)
            domain = parsed.netloc.replace("www.", "")
            base_api = f"https://api.{domain}/api/v1/series/{slug}/chapters"

            headers = {
                "Accept": "application/json",
                "Referer": url,
                "User-Agent": self.headers.get("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"),
            }

            res = {}
            async with aiohttp.ClientSession(headers=headers) as session:
                try:
                    async with session.get(f"{base_api}?page=1", timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status == 200:
                            data = await r.json()
                            if isinstance(data, dict):
                                total_pages = int(data.get("totalPages", 1) or 1)
                                for item in data.get("data", []) or []:
                                    num = item.get("number")
                                    ch_slug = item.get("slug")
                                    is_free = item.get("isFree", True)
                                    requires_purchase = item.get("requiresPurchase", False)
                                    price = item.get("price", 0)
                                    if num is not None and ch_slug:
                                        ch_url = f"https://{domain}/series/{slug}/{ch_slug}"
                                        locked = (not is_free) or requires_purchase or (price > 0)
                                        res[float(num)] = {
                                            "url": ch_url,
                                            "locked": locked,
                                            "reason": f"api:price={price},isFree={is_free}" if locked else "api:free",
                                        }

                                if total_pages > 1:
                                    async def _fetch_p(p_idx):
                                        try:
                                            async with session.get(f"{base_api}?page={p_idx}", timeout=aiohttp.ClientTimeout(total=10)) as rp:
                                                if rp.status == 200:
                                                    p_data = await rp.json()
                                                    return p_data.get("data", []) or []
                                        except Exception as err:
                                            print(f"[Qimanhwa] Error fetching page {p_idx}: {err}")
                                        return []

                                    page_tasks = [_fetch_p(p) for p in range(2, total_pages + 1)]
                                    all_pages_items = await asyncio.gather(*page_tasks)
                                    for page_items in all_pages_items:
                                        for item in page_items:
                                            num = item.get("number")
                                            ch_slug = item.get("slug")
                                            is_free = item.get("isFree", True)
                                            requires_purchase = item.get("requiresPurchase", False)
                                            price = item.get("price", 0)
                                            if num is not None and ch_slug:
                                                ch_url = f"https://{domain}/series/{slug}/{ch_slug}"
                                                locked = (not is_free) or requires_purchase or (price > 0)
                                                res[float(num)] = {
                                                    "url": ch_url,
                                                    "locked": locked,
                                                    "reason": f"api:price={price},isFree={is_free}" if locked else "api:free",
                                                }
                except Exception as e:
                    print(f"[Qimanhwa] Paginated API fetch failed: {e}")

            if res:
                return dict(sorted(res.items()))

            # Fallback 1 & 2: HTML Scraping
            loop = asyncio.get_event_loop()
            html = await loop.run_in_executor(
                None, lambda: self.fetch_html(url)
            )
            if html:
                res = self._extract_locked_chapters_from_html(html, url)
                if res:
                    return dict(sorted(res.items()))

        except Exception as e:
            print(f"[Qimanhwa] get_chapters_with_lock_info error: {e}")

        # Fallback to get_all_chapters if nothing else works (marking all as free)
        chapters = await self.get_all_chapters(url)
        return {
            num: {"url": ch_url, "locked": False, "reason": "fallback-no-lock-data"}
            for num, ch_url in sorted(chapters.items())
        }

    def _extract_locked_chapters_from_html(self, html: str, url: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        chapters = {}
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        domain = parsed.netloc.replace("www.", "")
        series_slug = self._extract_slug(url)

        # 1. Try TransferState script fallback
        for script in soup.find_all("script"):
            content = script.string or ""
            if '"chapters"' in content or '"slug"' in content:
                matches = re.findall(r"\{.*\}", content)
                for m in matches:
                    try:
                        data = json.loads(m)
                        found_items = []
                        def find_chapters(d):
                            if isinstance(d, dict):
                                if "chapters" in d and isinstance(d["chapters"], list):
                                    found_items.extend(d["chapters"])
                                for v in d.values():
                                    find_chapters(v)
                            elif isinstance(d, list):
                                for item in d:
                                    find_chapters(item)
                        find_chapters(data)
                        if found_items:
                            for item in found_items:
                                num = item.get("chapter_number") or item.get("number")
                                ch_slug = item.get("slug")
                                is_free = item.get("isFree", item.get("is_free", True))
                                price = item.get("price", 0)
                                if num is not None:
                                    ch_url = f"https://{domain}/series/{series_slug}/{ch_slug}" if ch_slug else f"https://{domain}/series/{series_slug}/chapter/{num}"
                                    locked = (not is_free) or (price > 0)
                                    chapters[float(num)] = {
                                        "url": ch_url,
                                        "locked": locked,
                                        "reason": f"ssr:price={price}" if locked else "ssr:free"
                                    }
                            if chapters:
                                return chapters
                    except Exception:
                        continue

        # 2. Try HTML anchor tags extraction
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("http"):
                href = urljoin(base, href)
            m = re.search(r"chapter[-/](\d+(?:\.\d+)?)", href, re.I)
            if m and (domain in href):
                num = float(m.group(1))
                if num not in chapters:
                    price_span = a.select_one(".cl-price")
                    locked = False
                    reason = "html:free"
                    if price_span:
                        price_text = price_span.get_text(strip=True)
                        locked = True
                        reason = f"html:price={price_text}"
                    else:
                        if "coin" in a.get_text().lower() or a.select_one("app-flame-icon"):
                            locked = True
                            reason = "html:flame-icon"
                    chapters[num] = {
                        "url": href,
                        "locked": locked,
                        "reason": reason
                    }

        return dict(sorted(chapters.items()))

    def get_latest_chapter(self, url: str):
        import asyncio

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(result.keys()) if result else None
