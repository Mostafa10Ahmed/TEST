import cloudscraper
from bs4 import BeautifulSoup
from .base_provider import BaseProvider
import re
from urllib.parse import urljoin, urlparse

class WebtoonsProvider(BaseProvider):
    def __init__(self):
        self.scraper = cloudscraper.create_scraper()
        self.headers = {
            'Referer': 'https://www.webtoons.com/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

    async def get_images(self, url):
        try:
            # موقع Webtoons يحتاج Referer دقيق جداً لكل فصل
            headers = self.headers.copy()
            headers['Referer'] = url
            
            response = self.scraper.get(url, headers=headers, timeout=15)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # الصور موجودة في الـ viewer_img_area
            container = soup.select_one('#_img_viewer_area') or soup.select_one('.viewer_img')
            if not container:
                return []
                
            img_tags = container.find_all('img')
            images = []
            for img in img_tags:
                # ميزة تعلمناها من المستودع: data-url هي الرابط الأصلي
                src = img.get('data-url') or img.get('src')
                if src:
                    # تحسين الجودة: إزالة تقليل الحجم إذا وجد
                    src = src.split('?')[0] 
                    images.append(src.strip())
            return images
        except Exception as e:
            print(f"Webtoons images error: {e}")
            return []

    async def get_chapters_with_lock_info(self, series_url: str) -> dict:
        """جلب الفصول مع كشف الفصول المقفلة (Daily Pass / Coins)"""
        try:
            all_chs = await self.get_all_chapters(series_url)

            response = self.scraper.get(series_url, headers=self.headers, timeout=15)
            soup = BeautifulSoup(response.text, 'html.parser')

            locked_nums = set()
            def check_soup(s):
                items = s.select('#_listUl li')
                for li in items:
                    is_locked = bool(li.select('.ico_lock, .ico_pay, .ico_clock, .ico_waiting, .ico_pass'))
                    if is_locked:
                        a = li.find('a')
                        if a:
                            href = a.get('href', '')
                            m = re.search(r'episode_no=(\d+)', href)
                            if m:
                                locked_nums.add(float(m.group(1)))

            check_soup(soup)
            # التحقق من الصفحات الأخرى للأقفال
            for page in range(2, 5):
                p_url = f"{series_url.rstrip('/')}&page={page}" if '?' in series_url else f"{series_url.rstrip('/')}?page={page}"
                try:
                    r = self.scraper.get(p_url, headers=self.headers, timeout=10)
                    if r.status_code == 200:
                        check_soup(BeautifulSoup(r.text, 'html.parser'))
                    else: break
                except: break

            result = {}
            for num, url in all_chs.items():
                result[num] = {
                    "url": url,
                    "locked": num in locked_nums
                }
            return result
        except Exception as e:
            print(f"Webtoons lock info error: {e}")
            chs = await self.get_all_chapters(series_url)
            return {n: {"url": u, "locked": False} for n, u in chs.items()}

    async def get_all_chapters(self, series_url):
        try:
            parsed_url = urlparse(series_url)
            from urllib.parse import parse_qs
            qs = parse_qs(parsed_url.query)
            title_no = qs.get('title_no', [None])[0]
            
            if not title_no:
                match = re.search(r'title_no=(\d+)', series_url)
                if match: title_no = match.group(1)

            if title_no:
                webtoon_type = "canvas" if "canvas" in series_url else "webtoon"
                api_url = f"https://m.webtoons.com/api/v1/{webtoon_type}/{title_no}"
                
                try:
                    mobile_headers = self.headers.copy()
                    mobile_headers['User-Agent'] = 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1'
                    resp = self.scraper.get(api_url, headers=mobile_headers, timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        episodes = data.get('result', {}).get('episodeList', [])
                        chapters = {}
                        for ep in episodes:
                            num = ep.get('episodeNo')
                            viewer_path = ep.get('viewerLink')
                            if viewer_path:
                                ch_url = urljoin("https://www.webtoons.com", viewer_path)
                                chapters[float(num)] = ch_url
                        if chapters: return chapters
                except: pass

            def _extract(html, b_url):
                s = BeautifulSoup(html, 'html.parser')
                res = {}
                items = s.select('#_listUl li')
                for li in items:
                    a = li.find('a')
                    if not a: continue
                    href = a.get('href')
                    ep_match = re.search(r'episode_no=(\d+)', href)
                    if ep_match:
                        num = float(ep_match.group(1))
                        res[num] = urljoin(b_url, href)
                return res

            response = self.scraper.get(series_url, headers=self.headers, timeout=15)
            chapters = _extract(response.text, series_url)

            extra = self._paginate_chapters(series_url, _extract)
            chapters.update(extra)

            return chapters
        except Exception as e:
            print(f"Webtoons chapters error: {e}")
            return {}
