import cloudscraper
from bs4 import BeautifulSoup
from .base_provider import BaseProvider
import re
import json

class ManganatoProvider(BaseProvider):
    def __init__(self):
        self.scraper = cloudscraper.create_scraper()
        self.headers = {
            'Referer': 'https://manganato.com/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

    async def get_images(self, url):
        try:
            response = self.scraper.get(url, headers=self.headers, timeout=15)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # سلكتورز شاملة لمانجا ناتو وكاكا لوت
            container = soup.select_one('.container-chapter-reader') or soup.select_one('.v-wrap-full')
            if not container:
                return []
                
            img_tags = container.find_all('img')
            images = []
            for img in img_tags:
                src = img.get('src') or img.get('data-src')
                if src and 'http' in src:
                    images.append(src.strip())
            return images
        except Exception as e:
            print(f"Manganato images error: {e}")
            return []

    async def get_all_chapters(self, series_url):
        try:
            # استخراج الـ ID من الرابط
            # مثال: https://manganato.com/manga-bn978870 -> bn978870
            match = re.search(r'manga-([a-z0-9]+)', series_url)
            if match:
                manga_id = match.group(1)
                # استخدام الـ API الذي اكتشفناه من المستودع!
                api_url = f"https://www.manganato.gg/api/manga/manga-{manga_id}/chapters?limit=-1"
                try:
                    resp = self.scraper.get(api_url, headers=self.headers, timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        chapters_data = data.get('data', {}).get('chapters', [])
                        chapters = {}
                        for ch in chapters_data:
                            num = ch.get('chapter_num')
                            slug = ch.get('chapter_slug')
                            # بناء الرابط الكامل
                            ch_url = f"{series_url.rstrip('/')}/{slug}"
                            chapters[float(num)] = ch_url
                        if chapters: return chapters
                except: pass

            # Fallback للطريقة العادية إذا فشل الـ API
            response = self.scraper.get(series_url, headers=self.headers, timeout=15)
            soup = BeautifulSoup(response.text, 'html.parser')
            chapters = {}
            chapter_list = soup.select('.row-content-chapter li a') or soup.select('.chapter-list a')
            for a in chapter_list:
                href = a.get('href')
                text = a.text.lower()
                num_match = re.search(r'chapter\s*([\d.]+)', text)
                if num_match:
                    chapters[float(num_match.group(1))] = href
            return chapters
        except Exception as e:
            print(f"Manganato chapters error: {e}")
            return {}
