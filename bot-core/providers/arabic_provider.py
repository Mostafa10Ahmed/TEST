import re
import json
import asyncio
from bs4 import BeautifulSoup
from .base_provider import BaseProvider
from urllib.parse import urljoin, urlparse


class ArabicProvider(BaseProvider):
    """
    مزود متخصص للمواقع العربية:
    - mangalek.com
    - 3asq.to / 3asq.net
    - manga-ar.com / mangaarab.com
    - arabsama.com / mangaae.com
    - ozulscans.com (عربي)
    - mangat.to (عربي)
    يدعم بنية Madara WordPress والبنى المخصصة.
    """

    def __init__(self, scraper=None):
        super().__init__(scraper)

    def _base(self, url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url, {"Referer": self._base(url) + "/"})
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")
            images = []

            # 1. __NEXT_DATA__
            nd = soup.find("script", id="__NEXT_DATA__")
            if nd:
                try:
                    images = self._regex_images(json.dumps(json.loads(nd.string)))
                    if images:
                        return images
                except Exception:
                    pass

            # 2. سلكتورات Madara + مخصصة عربية
            selectors = [
                "div.reading-content img",
                "div.page-break img",
                "div#chapter-images img",
                "div.wp-manga-chapter-img img",
                "div.chapter-container img",
                "div.manga-reader img",
                "div#reader-images img",
                "div.pages-container img",
                "article.chapter img",
                "div.images-container img",
                "img[data-src]",
                "img.chapter-img",
            ]
            for sel in selectors:
                for img in soup.select(sel):
                    src = (img.get("data-src") or img.get("data-lazy-src") or
                           img.get("data-original") or img.get("src") or "").strip()
                    if not src:
                        continue
                    if src.startswith("//"):
                        src = "https:" + src
                    elif not src.startswith("http"):
                        src = urljoin(url, src)
                    if src.startswith("http") and src not in images:
                        if not (
                            any(x in src.lower() for x in ["logo", "banner", "icon", "avatar"])
                            or re.search(r'\bads\b', src.lower())
                        ):
                            images.append(src)
                if images:
                    return images

            # 3. سكريبتات JS
            for script in soup.find_all("script"):
                content = script.string or ""
                if any(x in content for x in [".webp", ".jpg", ".jpeg", ".png"]):
                    for img in self._regex_images(content):
                        if img not in images:
                            images.append(img)
            if images:
                return images

            return self._regex_images(html)
        except Exception as e:
            print(f"[Arabic] get_images error: {e}")
            return []

    def _regex_images(self, text: str) -> list:
        images = []
        for pat in [
            r'"src"\s*:\s*"(https?://[^"]+\.(?:webp|jpg|jpeg|png)[^"]*)"',
            r'"url"\s*:\s*"(https?://[^"]+\.(?:webp|jpg|jpeg|png)[^"]*)"',
            r'https?://[a-zA-Z0-9\-_.]+/[^"\'\s<>]+?\.(?:webp|jpg|jpeg|png)(?:\?[^"\'\s<>]*)?',
        ]:
            for m in re.findall(pat, text, re.IGNORECASE):
                src = m if isinstance(m, str) else m[0]
                src = src.replace("\\u002F", "/").replace("\\", "").strip().rstrip('"')
                if src.startswith("http") and src not in images:
                    if not (
                        any(x in src.lower() for x in ["logo", "avatar", "icon", "banner"])
                        or re.search(r'\bads\b', src.lower())
                    ):
                        images.append(src)
        return images

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url)
            if not html:
                return {}
            soup = BeautifulSoup(html, "html.parser")
            base = self._base(series_url)
            chapters = {}

            # 1. __NEXT_DATA__
            nd = soup.find("script", id="__NEXT_DATA__")
            if nd:
                try:
                    text = json.dumps(json.loads(nd.string))
                    for m in re.finditer(
                        r'"((?:https?://[^"]+|/[^"]+)/(?:chapter|فصل)[s]?[/-](\d+(?:\.\d+)?)(?:[/"\\]))',
                        text, re.UNICODE
                    ):
                        href = m.group(1).replace("\\u002F", "/").replace("\\", "")
                        if not href.startswith("http"):
                            href = urljoin(base, href)
                        try:
                            chapters[float(m.group(2))] = href
                        except Exception:
                            pass
                    if chapters:
                        return chapters
                except Exception:
                    pass

            # 2. سلكتورات Madara
            for sel in [
                "li.wp-manga-chapter a",
                "div#chapterlist li a",
                "ul.main.version-chap li a",
                "div.eph-num a",
                "a[href*='/chapter']",
                "a[href*='chapter-']",
                "div.chapter-list a",
                "ul.chapter-list li a",
                "div.chapters a",
                "table.chapters-table a",
            ]:
                for a in soup.select(sel):
                    href = a.get("href", "").strip()
                    if not href:
                        continue
                    if not href.startswith("http"):
                        href = urljoin(series_url, href)
                    text_content = a.get_text()
                    m = (re.search(r'(?:chapter|فصل|الفصل|ch)\s*[:\-]?\s*([\d.]+)', text_content, re.I | re.UNICODE)
                         or re.search(r'(?:chapter|فصل)[s]?[/-]([\d.]+)', href, re.I | re.UNICODE))
                    if m:
                        try:
                            chapters[float(m.group(1))] = href
                        except Exception:
                            pass
                if chapters:
                    break

            # 3. AJAX Madara
            if not chapters:
                holder = soup.select_one("#manga-chapters-holder")
                post_id = holder.get("data-id") if holder else None
                if not post_id:
                    m = re.search(r'manga_id\s*:\s*(\d+)', str(soup))
                    if m:
                        post_id = m.group(1)
                if post_id:
                    ajax_url = f"{base}/wp-admin/admin-ajax.php"
                    try:
                        resp = self.scraper.post(
                            ajax_url,
                            data={"action": "manga_get_chapters", "manga": post_id},
                            headers=self.headers, timeout=15
                        )
                        if resp.status_code == 200 and resp.text.strip():
                            ajax_soup = BeautifulSoup(resp.text, "html.parser")
                            for a in ajax_soup.select("li.wp-manga-chapter a, div.eph-num a"):
                                href = a.get("href", "").strip()
                                if not href.startswith("http"):
                                    href = urljoin(series_url, href)
                                m = re.search(r'chapter[/-](\d+(?:\.\d+)?)', href, re.I)
                                if m:
                                    try:
                                        chapters[float(m.group(1))] = href
                                    except Exception:
                                        pass
                    except Exception:
                        pass

            return chapters
        except Exception as e:
            print(f"[Arabic] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(result.keys()) if result else None
