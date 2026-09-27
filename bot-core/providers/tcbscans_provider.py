import re
import json
from bs4 import BeautifulSoup
from .base_provider import BaseProvider
from urllib.parse import urljoin, urlparse


class TCBScansProvider(BaseProvider):
    """مزود TCBScans - موقع One Piece الرسمي للترجمة"""

    def __init__(self):
        super().__init__()
        self.base_url = "https://tcbscans.me"
        self.headers['Referer'] = self.base_url + '/'

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url, {'Referer': self.base_url + '/'})
            if not html:
                return []

            soup = BeautifulSoup(html, 'html.parser')
            images = []

            # TCBScans يستخدم بنية بسيطة
            for img in soup.select('.flex.flex-col.items-center img, .chapter-image img, img.w-full'):
                src = (img.get('src') or img.get('data-src') or '').strip()
                if src.startswith('http') and src not in images:
                    images.append(src)

            if not images:
                for img in soup.find_all('img'):
                    src = (img.get('src') or img.get('data-src') or '').strip()
                    if src.startswith('http') and any(x in src for x in ['cdn', 'storage', '/manga/', '/uploads/']):
                        if src not in images and not any(x in src.lower() for x in ['logo', 'icon', 'avatar']):
                            images.append(src)

            return images
        except Exception as e:
            print(f"[TCBScans] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url)
            if not html:
                return {}

            soup = BeautifulSoup(html, 'html.parser')
            chapters = {}
            parsed = urlparse(series_url)
            base = f"{parsed.scheme}://{parsed.netloc}"

            for a in soup.find_all('a', href=True):
                href = a['href']
                if '/chapters/' not in href and '/chapter-' not in href:
                    continue
                if not href.startswith('http'):
                    href = urljoin(base, href)
                m = re.search(r'chapter[-/](\d+(?:\.\d+)?)', href, re.I)
                if not m:
                    m = re.search(r'(\d+(?:\.\d+)?)\s*$', a.get_text().strip())
                if m:
                    num = float(m.group(1))
                    if num not in chapters:
                        chapters[num] = href

            return chapters
        except Exception as e:
            print(f"[TCBScans] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        import asyncio
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(result.keys()) if result else None
