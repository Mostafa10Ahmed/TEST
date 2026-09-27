import cloudscraper
from bs4 import BeautifulSoup
from .base_provider import BaseProvider
import re
from urllib.parse import urljoin, urlparse

class MangaBuffProvider(BaseProvider):
    def __init__(self):
        super().__init__()
        self.scraper = cloudscraper.create_scraper()
        self.headers = {
            'Referer': 'https://mangabuff.ru/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        }

    def _fetch_html_scrapling(self, url):
        # 1. Try scrapling with curl_cffi engine (very fast and bypassed anti-bots)
        try:
            import scrapling
            fetcher = scrapling.Fetcher(engine="curl_cffi")
            resp = fetcher.get(url)
            if resp.status == 200 and resp.html_content:
                html = str(resp.html_content)
                if len(html) > 500:
                    return html
        except Exception as e:
            print(f"[MangaBuff] Scrapling curl_cffi error: {e}")

        # 2. Try scrapling with stealth engine (playwright stealth browser)
        try:
            import scrapling
            fetcher = scrapling.Fetcher(engine="stealth")
            resp = fetcher.get(url)
            if resp.status == 200 and resp.html_content:
                html = str(resp.html_content)
                if len(html) > 500:
                    return html
        except Exception as e:
            print(f"[MangaBuff] Scrapling stealth error: {e}")

        # 3. Fallback to standard fetch_html (requests / curl_cffi inside BaseProvider)
        print(f"[MangaBuff] Falling back to standard fetch_html for: {url}")
        return self.fetch_html(url)

    async def get_images(self, url):
        try:
            html = self._fetch_html_scrapling(url)
            if not html:
                return []
            soup = BeautifulSoup(html, 'html.parser')
            
            img_tags = soup.select('.reader__pages img')
            images = []
            for img in img_tags:
                src = img.get('data-src') or img.get('src')
                if src:
                    src = src.strip()
                    images.append(urljoin(url, src))
            return images
        except Exception as e:
            print(f"MangaBuff images error: {e}")
            return []

    async def get_all_chapters(self, series_url):
        try:
            html = self._fetch_html_scrapling(series_url)
            if not html:
                return {}
            soup = BeautifulSoup(html, 'html.parser')
            
            parsed = urlparse(series_url)
            parts = [p for p in parsed.path.split('/') if p]
            if len(parts) < 2 or parts[0] != 'manga':
                print(f"MangaBuff invalid series url: {series_url}")
                return {}
            slug = parts[1]
            
            chapters = {}
            for a in soup.find_all('a', href=True):
                href = a.get('href')
                if not href:
                    continue
                
                abs_href = urljoin(series_url, href)
                href_parts = [p for p in urlparse(abs_href).path.split('/') if p]
                
                if len(href_parts) >= 4 and href_parts[0] == 'manga' and href_parts[1] == slug:
                    ch_part = href_parts[3]
                    try:
                        num = float(ch_part)
                        chapters[num] = abs_href
                    except ValueError:
                        num_match = re.search(r'(\d+(?:\.\d+)?)', ch_part)
                        if num_match:
                            try:
                                num = float(num_match.group(1))
                                chapters[num] = abs_href
                            except ValueError:
                                pass
            return chapters
        except Exception as e:
            print(f"MangaBuff chapters error: {e}")
            return {}
