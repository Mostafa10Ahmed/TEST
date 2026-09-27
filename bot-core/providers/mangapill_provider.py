import cloudscraper
from bs4 import BeautifulSoup
from .base_provider import BaseProvider
import re

class MangaPillProvider(BaseProvider):
    def __init__(self):
        self.scraper = cloudscraper.create_scraper()
        self.headers = {
            'Referer': 'https://mangapill.com/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36'
        }

    async def get_images(self, url):
        try:
            response = self.scraper.get(url, headers=self.headers, timeout=15)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # السلكتور الخاص بـ MangaPill من المستودع
            img_tags = soup.select('img[data-src]')
            if not img_tags:
                img_tags = soup.select('img') # Fallback
                
            images = [img.get('data-src') or img.get('src') for img in img_tags if (img.get('data-src') or img.get('src'))]
            return [img.strip() for img in images if img and 'http' in img]
        except Exception as e:
            print(f"MangaPill images error: {e}")
            return []

    async def get_all_chapters(self, series_url):
        try:
            response = self.scraper.get(series_url, headers=self.headers, timeout=15)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            chapters = {}
            # جلب الفصول من MangaPill
            chapter_elements = soup.select('#chapters a')
            for a in chapter_elements:
                href = a.get('href')
                if href and not href.startswith('http'):
                    href = 'https://mangapill.com' + href
                
                text = a.text.lower()
                match = re.search(r'chapter\s*([\d.]+)', text)
                if match:
                    ch_num = float(match.group(1))
                    chapters[ch_num] = href
            
            return chapters
        except Exception as e:
            print(f"MangaPill chapters error: {e}")
            return {}
