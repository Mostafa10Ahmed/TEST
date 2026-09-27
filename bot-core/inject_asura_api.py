import re

with open('providers/asura_provider.py', 'r', encoding='utf-8') as f:
    code = f.read()

new_get_images = '''
    async def get_images(self, url: str) -> List[str]:
        try:
            images = []
            html = self.fetch_html(url)
            
            # If we have an access_token, we can try fetching the API directly
            cookies = SITE_AUTH.get("asurascans.com", {})
            if "access_token" in cookies:
                try:
                    # extract slug from url
                    # https://asurascans.com/comics/initializing-the-sect-system-7b57f74d/chapter/41
                    m = re.search(r'/comics/([^/]+)/chapter/([^/]+)', url)
                    if m:
                        series_slug = m.group(1)
                        chapter_slug = m.group(2)
                        api_url = f"https://api.asurascans.com/api/series/{series_slug}/chapters/{chapter_slug}"
                        
                        headers = {
                            "Authorization": f"Bearer {cookies['access_token']}",
                            "Accept": "application/json"
                        }
                        if "__custom_user_agent" in cookies:
                            headers["User-Agent"] = cookies["__custom_user_agent"]
                        
                        resp = self.fetch_json(api_url, extra_headers=headers)
                        if resp and isinstance(resp, dict) and "data" in resp:
                            data = resp["data"]
                            if "pages" in data and data["pages"]:
                                for p in data["pages"]:
                                    src = p.get("url")
                                    if src and src.startswith("http"):
                                        images.append(src)
                                if images:
                                    return images
                except Exception as e:
                    print(f"[AsuraScans API error] {e}")

            if not html:
                return []

            # fallback to HTML parsing
            data = self._extract_rsc_data(html)
            if data and 'chapter' in data and 'pages' in data['chapter']:
                for page in data['chapter']['pages']:
                    src = page.get('url')
                    if src and src.startswith('http') and src not in images:
                        images.append(src)
                if images:
                    return images

            soup = BeautifulSoup(html, 'html.parser')
            reader_divs = [
                soup.find('div', id='readerarea'),
                soup.select_one('.reading-content'),
                soup.select_one('[class*="reader"]'),
                soup.select_one('[id*="reader"]'),
                soup.select_one('.chapter-content'),
            ]
            for div in reader_divs:
                if not div:
                    continue
                for img in div.find_all('img'):
                    src = (img.get('data-src') or img.get('src') or '').strip()
                    if src.startswith('http') and src not in images:
                        if not any(x in src.lower() for x in ['logo', 'avatar', 'icon']):
                            images.append(src)
                if images:
                    return images

            if not images:
                images = self._extract_images_from_json(html)

            return images
        except Exception as e:
            print(f"[AsuraScans] get_images error: {e}")
            return []
'''

pattern = r'    async def get_images[\s\S]*?(?=    def _extract_images_from_json)'
code = re.sub(pattern, new_get_images + '\n', code)

with open('providers/asura_provider.py', 'w', encoding='utf-8') as f:
    f.write(code)

print("Injected API fetch logic into asura get_images")
