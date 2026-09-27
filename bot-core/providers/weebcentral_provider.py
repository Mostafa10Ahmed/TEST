import cloudscraper
from bs4 import BeautifulSoup
from .base_provider import BaseProvider
import re
from urllib.parse import urljoin, urlparse

class WeebCentralProvider(BaseProvider):
    def __init__(self):
        self.scraper = cloudscraper.create_scraper()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        }

    async def get_images(self, url):
        try:
            # إضافة ستايل القراءة الطويلة لجلب كل الصور دفعة واحدة
            if 'reading_style=long_strip' not in url:
                url = f"{url}/images?reading_style=long_strip" if '?' not in url else f"{url}&reading_style=long_strip"
            
            headers = self.headers.copy()
            headers['Referer'] = url
            
            response = self.scraper.get(url, headers=headers, timeout=20)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # الصور في WeebCentral تكون عادة داخل tags img مباشرة في صفحة الصور
            img_tags = soup.find_all('img')
            images = []
            for img in img_tags:
                src = img.get('src')
                # تصفية الصور التي ليست جزءاً من الفصل (أيقونات، لوغو، إلخ)
                if src and src.startswith('http') and 'broken_image' not in src:
                    # استبعاد صور الأفاتار أو الأيقونات الصغيرة
                    if any(x in src.lower() for x in ['logo', 'icon', 'avatar', 'cover']):
                        continue
                    images.append(src.strip())
            
            return images
        except Exception as e:
            print(f"WeebCentral images error: {e}")
            return []

    async def get_all_chapters(self, series_url):
        try:
            # نحتاج رابط القائمة الكاملة
            # الرابط الأصلي: https://weebcentral.com/series/01J4HXP.../Tower-of-God
            # رابط الفصول: https://weebcentral.com/series/01J4HXP.../full-chapter-list
            
            chapter_list_url = series_url
            if 'full-chapter-list' not in series_url:
                parsed = urlparse(series_url)
                parts = parsed.path.split('/')
                # عادة الأجزاء هي ['', 'series', 'ID', 'Name']
                if len(parts) >= 3:
                    new_path = f"/series/{parts[2]}/full-chapter-list"
                    chapter_list_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", new_path)

            response = self.scraper.get(chapter_list_url, headers=self.headers, timeout=20)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            chapters = {}
            # السلكتور المكتشف من الكود المصدري
            items = soup.select('div[x-data] > a') or soup.select('a[href*="/chapters/"]')
            
            for a in items:
                href = a.get('href')
                if not href: continue
                
                # استخراج اسم الفصل (عادة يكون داخل span)
                name_tag = a.select_one('span.flex > span') or a
                name_text = name_tag.text.strip()
                
                # محاولة استخراج رقم الفصل من النص
                # مثال: "Chapter 580" -> 580
                num_match = re.search(r'(?:Chapter|Ch\.)\s*(\d+\.?\d*)', name_text, re.I)
                if num_match:
                    num = float(num_match.group(1))
                    chapters[num] = urljoin(series_url, href)
                else:
                    # إذا لم نجد رقم، نستخدم ترتيب الفصل كبديل
                    pass

            # ترتيب الفصول تصاعدياً
            return chapters
        except Exception as e:
            print(f"WeebCentral chapters error: {e}")
            return {}
