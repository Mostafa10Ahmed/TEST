import re
import json
import asyncio
from bs4 import BeautifulSoup
from .base_provider import BaseProvider
from urllib.parse import urljoin, urlparse


class BatoProvider(BaseProvider):
    """
    مزود Bato.to — scraping + استخراج بيانات JSON مدمجة.
    يدعم: bato.to, dto.to, batotoo.com
    """

    def __init__(self):
        super().__init__()

    def _extract_series_id(self, url: str) -> str:
        m = re.search(r'/series/(\d+)', url)
        return m.group(1) if m else None

    def _extract_chapter_id(self, url: str) -> str:
        m = re.search(r'/chapter/(\d+)', url)
        return m.group(1) if m else None

    def _base(self, url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url)
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")

            # طريقة 1: __NEXT_DATA__
            nd = soup.find("script", id="__NEXT_DATA__")
            if nd:
                try:
                    data = json.loads(nd.string)
                    text = json.dumps(data)
                    images = self._extract_images_from_json(text)
                    if images:
                        return images
                except Exception:
                    pass

            # طريقة 2: astroData أو باقي السكريبتات
            for script in soup.find_all("script"):
                content = script.string or ""
                if "imgHttps" in content or "imageFiles" in content or "bato.to/images" in content:
                    images = self._extract_images_from_text(content)
                    if images:
                        return images

            # طريقة 3: img tags مباشرة
            images = []
            base = self._base(url)
            for img in soup.select("div.page-load img, div#chapter-images img, img[class*='page']"):
                src = img.get("data-src") or img.get("src") or ""
                if src.startswith("//"):
                    src = "https:" + src
                elif not src.startswith("http"):
                    src = urljoin(base, src)
                if src.startswith("http") and src not in images:
                    images.append(src)
            return images
        except Exception as e:
            print(f"[Bato] get_images error: {e}")
            return []

    def _extract_images_from_json(self, text: str) -> list:
        images = []
        for pattern in [
            r'"(https?://[^"]+bato[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
            r'"(https?://img[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
            r'"imgHttps"\s*:\s*"([^"]+)"',
            r'"imageFiles"\s*:\s*\[([^\]]+)\]',
        ]:
            for m in re.findall(pattern, text, re.IGNORECASE):
                if isinstance(m, list):
                    for item in m:
                        item = item.strip().strip('"')
                        if item.startswith("http") and item not in images:
                            images.append(item)
                else:
                    src = m.replace("\\u002F", "/").replace("\\", "").strip()
                    if src.startswith("http") and src not in images:
                        images.append(src)
        return images

    def _extract_images_from_text(self, text: str) -> list:
        images = []
        for m in re.findall(r'"(https?://[^"]+\.(?:jpg|jpeg|png|webp)(?:\?[^"]*)?)"', text, re.I):
            src = m.replace("\\u002F", "/").replace("\\", "").strip()
            if src.startswith("http") and src not in images:
                if not any(x in src.lower() for x in ["logo", "banner", "avatar", "icon"]):
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

            # طريقة 1: __NEXT_DATA__
            nd = soup.find("script", id="__NEXT_DATA__")
            if nd:
                try:
                    text = json.dumps(json.loads(nd.string))
                    for m in re.finditer(
                        r'"((?:https?://[^"]+|/)[^"]*chapter/(\d+(?:\.\d+)?)[^"]*)"', text
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

            # طريقة 2: روابط HTML
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not href.startswith("http"):
                    href = urljoin(base, href)
                m = re.search(r'/chapter/(\d+(?:\.\d+)?)', href)
                if m and base.split("//")[1].split("/")[0] in href:
                    try:
                        chapters[float(m.group(1))] = href
                    except Exception:
                        pass
            return chapters
        except Exception as e:
            print(f"[Bato] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(result.keys()) if result else None
