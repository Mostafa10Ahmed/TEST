"""
مزودات RAW الأصلية:
- AC.QQ (腾讯漫画 / Tencent Comics)
- Kuaikan (快看漫画)
- LINE Manga (manga.line.me)
- Piccoma (piccoma.com / piccoma.jp)
- Comico (comico.jp / comico.kr)
- iQiyi Manhua (manhua.iqiyi.com)
- Naver (نسخة محسّنة للفصول المجانية)
- Lezhin (lezhin.com — المحتوى المجاني فقط)
- Webtoon (webtoons.com — تحسين المزود الموجود)
"""

import re
import json
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from .base_provider import BaseProvider, fetch_with_curl
from urllib.parse import urljoin, urlparse


# ─────────────────────────────────────────────────────────────────
#  AC.QQ — 腾讯漫画 (Tencent Comics)
# ─────────────────────────────────────────────────────────────────
class AcQQProvider(BaseProvider):
    """
    مزود AC.QQ (ac.qq.com) — كوميكس Tencent الصينية.
    """

    BASE = "https://ac.qq.com"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer":       "https://ac.qq.com/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    def _extract_comic_id(self, url: str) -> str:
        m = re.search(r'/Comic(?:Info)?/index/id/(\d+)', url)
        if not m:
            m = re.search(r'comicId=(\d+)', url)
        if not m:
            m = re.search(r'/(\d+)(?:/|$)', url)
        return m.group(1) if m else None

    def _extract_chapter_info(self, url: str):
        m = re.search(r'/ComicView/index/id/(\d+)/cid/(\d+)', url)
        return (m.group(1), m.group(2)) if m else (None, None)

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url) or fetch_with_curl(url, referer="https://ac.qq.com/")
            if not html:
                return []
            
            # 1. Decode from JS DATA variable
            m_data = re.search(r"var\s+DATA\s*=\s*'([^']+)'", html)
            if m_data:
                data_str = m_data.group(1)
                nonce_lines = []
                for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S):
                    for line in s.split("\n"):
                        if "window[" in line and "nce" in line:
                            nonce_lines.append(line)
                nonce_joined = "\n".join(nonce_lines)
                node_runner = f"""
                const window = {{ Array: Array, DATA: {json.dumps(data_str)}, location: {{}} }};
                const W = window;
                const document = {{ 
                    getElementsByTagName: (tag) => [{{ tagName: tag }}],
                    getElementById: (id) => ({{ id }}),
                }};
                const navigator = {{ userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" }};
                {nonce_joined}
                
                function Base(){{
                    _keyStr="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=";
                    this.decode=function(c){{
                        var a="",b,d,h,f,g,e=0;
                        for(c=c.replace(/[^A-Za-z0-9\\+\\/\\=]/g,"");e<c.length;)
                            b=_keyStr.indexOf(c.charAt(e++)),
                            d=_keyStr.indexOf(c.charAt(e++)),
                            f=_keyStr.indexOf(c.charAt(e++)),
                            g=_keyStr.indexOf(c.charAt(e++)),
                            b=b<<2|d>>4,
                            d=(d&15)<<4|f>>2,
                            h=(f&3)<<6|g,
                            a+=String.fromCharCode(b),
                            64!=f&&(a+=String.fromCharCode(d)),
                            64!=g&&(a+=String.fromCharCode(h));
                        return a=_utf8_decode(a)
                    }};
                    _utf8_decode=function(c){{
                        for(var a="",b=0,d=c1=c2=0;b<c.length;)
                            d=c.charCodeAt(b),
                            128>d?(a+=String.fromCharCode(d),b++):191<d&&224>d?(c2=c.charCodeAt(b+1),a+=String.fromCharCode((d&31)<<6|c2&63),b+=2):(c2=c.charCodeAt(b+1),c3=c.charCodeAt(b+2),a+=String.fromCharCode((d&15)<<12|(c2&63)<<6|c3&63),b+=3);
                        return a
                    }}
                }}

                var B=new Base(),
                    T=W['DATA'].split(''),
                    N=W['nonce'],
                    len,locate,str;
                if (N) {{
                    N=N.match(/\\d+[a-zA-Z]+/g);
                    if (N) {{
                        len=N.length;
                        while(len-- && len >= 0){{
                            locate=parseInt(N[len])&255;
                            str=N[len].replace(/\\d+/g,'');
                            T.splice(locate,str.length);
                        }}
                    }}
                }}
                T=T.join('');
                try {{
                    const rawDecoded = B.decode(T);
                    console.log(rawDecoded);
                }} catch (e) {{
                    console.error(e);
                }}
                """
                try:
                    import subprocess
                    res = subprocess.run(["node"], input=node_runner, capture_output=True, text=True, encoding="utf-8", timeout=5)
                    if res.returncode == 0 and res.stdout.strip():
                        s = res.stdout.strip()
                        # Try parsing JSON first
                        first_brace = s.find("{")
                        last_brace = s.rfind("}")
                        if first_brace != -1 and last_brace != -1:
                            try:
                                obj = json.loads(s[first_brace:last_brace+1])
                                pics = obj.get("picture", [])
                                urls = [p.get("url") for p in pics if p.get("url")]
                                if urls:
                                    return urls
                            except Exception:
                                pass
                        
                        # Fallback: Extract acimg image URLs via regex from decoded string
                        raw_urls = re.findall(r'https?://[^\s"\'<>]+\.acimg\.cn[^\s"\'<>]*', s)
                        cleaned_urls = [u.replace('\\/', '/') for u in raw_urls]
                        if cleaned_urls:
                            return list(dict.fromkeys(cleaned_urls))
                except Exception:
                    pass

                # 1b. Pure Python deobfuscation fallback (works even without Node.js)
                try:
                    nonces = []
                    for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S):
                        for line in s.split("\n"):
                            if "window[" in line and "nce" in line:
                                m_val = re.search(r'=\s*(?:[^\'"]*[\'"])+([a-zA-Z0-9]+)[\'"]', line)
                                if m_val:
                                    nonces.append(m_val.group(1))
                    if nonces:
                        import base64
                        T = list(data_str)
                        for nonce in nonces:
                            N = re.findall(r'\d+[a-zA-Z]+', nonce)
                            if N:
                                for item in reversed(N):
                                    m_num = re.match(r'(\d+)', item)
                                    if m_num:
                                        locate = int(m_num.group(1)) & 255
                                        str_len = len(re.sub(r'\d+', '', item))
                                        del T[locate : locate + str_len]
                        T_str = "".join(T)
                        rem = len(T_str) % 4
                        if rem > 0:
                            T_str += "=" * (4 - rem)
                        raw_bytes = base64.b64decode(T_str)
                        decoded = raw_bytes.decode('utf-8', errors='ignore')
                        raw_urls = re.findall(r'https?:\\?/\\?/[^\s"\'<>]+\.acimg\.cn[^\s"\'<>]*', decoded)
                        cleaned_urls = [u.replace('\\/', '/') for u in raw_urls]
                        cleaned_urls = list(dict.fromkeys(cleaned_urls))
                        if cleaned_urls:
                            return cleaned_urls
                except Exception:
                    pass

            # 2. Fallback: regex search for acimg.cn URLs in HTML
            found_imgs = []
            for m in re.finditer(r'https?:\\?/\\?/[^"\'\s]+manhua\.acimg\.cn[^"\'\s]+', html):
                clean_url = m.group(0).replace(r'\/', '/').replace('\\', '').split('.jpg')[0] + '.jpg/0'
                if clean_url not in found_imgs:
                    found_imgs.append(clean_url)
            if found_imgs:
                return found_imgs
                
            # 3. Fallback: json pattern
            images = re.findall(
                r'"url"\s*:\s*"(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', html
            )
            return [img.replace("\\u002F", "/") for img in images]
        except Exception as e:
            print(f"[AcQQ] get_images error: {e}")
            return []

    async def get_chapters_with_lock_info(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url) or fetch_with_curl(series_url, referer="https://ac.qq.com/")
            soup = BeautifulSoup(html or "", "html.parser")
            
            container = soup.select_one(".works-chapter-list")
            items = container.select("span.works-chapter-item") if container else soup.select("span.works-chapter-item")
            
            if not items and hasattr(self, "playwright") and self.playwright:
                print(f"[AcQQ] Fallback to Playwright HTML fetch for: {series_url}")
                html = await self.playwright.fetch_html_playwright(series_url)
                if html:
                    soup = BeautifulSoup(html, "html.parser")
                    container = soup.select_one(".works-chapter-list")
                    items = container.select("span.works-chapter-item") if container else soup.select("span.works-chapter-item")

            base = self.BASE
            chapters = {}
            seen_cids = set()
            current_main_num = 0.0
            
            if items:
                for span in items:
                    a = span.select_one("a")
                    if not a:
                        continue
                    href = a.get("href", "")
                    if not href.startswith("http"):
                        href = urljoin(base, href)
                    m = re.search(r'/cid/(\d+)', href)
                    if not m:
                        continue
                    cid = m.group(1)
                    if cid in seen_cids:
                        continue
                    seen_cids.add(cid)
                    
                    txt = a.get_text(strip=True)
                    locked = bool(span.select_one(".ui-icon-pay"))
                    
                    # Filter notices vs chapters
                    is_notice = any(k in txt for k in ["公告", "重磅上线", "上线", "呼吁", "呼 吁", "停更", "休更", "活动", "中奖"])
                    is_extra = any(k in txt for k in ["特别篇", "番外", "外传", "后记", "预告", "序章", "前传"])
                    
                    m_num = re.search(r'第\s*(\d+(?:\.\d+)?)\s*[话回章集卷]', txt)
                    if not m_num:
                        m_num = re.search(r'(\d+(?:\.\d+)?)', txt)
                        
                    if m_num and not is_notice:
                        extracted = float(m_num.group(1))
                        if is_extra:
                            num = current_main_num + (extracted / 100.0) if current_main_num > 0 else extracted + 0.5
                        else:
                            num = extracted
                            current_main_num = num
                    elif is_extra:
                        num = current_main_num + 0.5 if current_main_num > 0 else 0.5
                    elif is_notice:
                        continue
                    else:
                        current_main_num += 1.0
                        num = current_main_num
                        
                    while num in chapters:
                        num = round(num + 0.01, 2)
                        
                    chapters[num] = {
                        "url": href,
                        "title": txt,
                        "locked": locked,
                        "reason": "acqq-icon" if locked else "free",
                    }
            else:
                for a in soup.select("a[href*='/ComicView/']"):
                    href = a.get("href", "")
                    if not href.startswith("http"):
                        href = urljoin(base, href)
                    m = re.search(r'/cid/(\d+)', href)
                    if not m:
                        continue
                    cid = m.group(1)
                    if cid in seen_cids:
                        continue
                    seen_cids.add(cid)
                    txt = a.get_text(strip=True)
                    current_main_num += 1.0
                    chapters[current_main_num] = {
                        "url": href,
                        "title": txt,
                        "locked": False,
                        "reason": "acqq-legacy-fallback",
                    }
            return chapters
        except Exception as e:
            print(f"[AcQQ] get_chapters_with_lock_info error: {e}")
            return {}

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            lock_info = await self.get_chapters_with_lock_info(series_url)
            return {num: item["url"] for num, item in lock_info.items()}
        except Exception as e:
            print(f"[AcQQ] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        r    = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(r.keys()) if r else None


# ─────────────────────────────────────────────────────────────────
#  Kuaikan — 快看漫画
# ─────────────────────────────────────────────────────────────────
class KuaikanProvider(BaseProvider):
    """
    مزود Kuaikan Manga (kuaikan.com / kuaikanmanhua.com).
    """

    API  = "https://www.kuaikanmanhua.com/api/v1"
    BASE = "https://www.kuaikanmanhua.com"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer":       "https://www.kuaikanmanhua.com/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    def _extract_topic_id(self, url: str) -> str:
        m = re.search(r'/web/topic/(\d+)', url)
        if not m:
            m = re.search(r'/webs/topic-next/(\d+)', url)
        if not m:
            m = re.search(r'/topic/(\d+)', url)
        if not m:
            m = re.search(r'/(\d+)(?:/|$)', url)
        return m.group(1) if m else None

    def _extract_comic_id(self, url: str) -> str:
        m = re.search(r'/web/comic/(\d+)', url)
        if not m:
            m = re.search(r'/webs/comic-next/(\d+)', url)
        if not m:
            m = re.search(r'/comic/(\d+)', url)
        return m.group(1) if m else None

    async def get_images(self, url: str):
        try:
            comic_id = self._extract_comic_id(url)
            if comic_id:
                for api_url in [f"https://api.kuaikanmanhua.com/v2/comic/{comic_id}", f"https://api.kuaikanmanhua.com/v1/comic/{comic_id}"]:
                    try:
                        raw_json = fetch_with_curl(api_url) if fetch_with_curl else None
                        if raw_json:
                            data = json.loads(raw_json)
                            comic_info = data.get("data", {}) if isinstance(data, dict) else {}
                            images = comic_info.get("images", []) or comic_info.get("comic_images", [])
                            if images:
                                res = []
                                for img in images:
                                    if isinstance(img, str) and img.startswith("http"):
                                        res.append(img)
                                    elif isinstance(img, dict) and img.get("url"):
                                        res.append(img["url"])
                                if res:
                                    return res
                    except Exception:
                        pass
                
                async with aiohttp.ClientSession(headers=self.headers) as s:
                    for api_url in [f"https://api.kuaikanmanhua.com/v2/comic/{comic_id}", f"https://api.kuaikanmanhua.com/v1/comic/{comic_id}"]:
                        try:
                            async with s.get(api_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                                if r.status == 200:
                                    data = await r.json(content_type=None)
                                    comic_info = data.get("data", {}) if isinstance(data, dict) else {}
                                    images = comic_info.get("images", []) or comic_info.get("comic_images", [])
                                    if images:
                                        res = []
                                        for img in images:
                                            if isinstance(img, str) and img.startswith("http"):
                                                res.append(img)
                                            elif isinstance(img, dict) and img.get("url"):
                                                res.append(img["url"])
                                        if res:
                                            return res
                        except Exception as e:
                            print(f"[Kuaikan] API {api_url} failed: {e}")

            html = self.fetch_html(url)
            if not html:
                return []
            # NEXT_DATA أو متغيرات JS
            m = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.+?});', html, re.S)
            if m:
                try:
                    data = json.loads(m.group(1))
                    images = re.findall(
                        r'"url"\s*:\s*"(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
                        json.dumps(data)
                    )
                    if images:
                        return images
                except Exception:
                    pass
            return []
        except Exception as e:
            print(f"[Kuaikan] get_images error: {e}")
            return []

    async def get_chapters_with_lock_info(self, series_url: str) -> dict:
        try:
            # 1. Try Kuaikan REST API first (fast and reliable)
            topic_id = self._extract_topic_id(series_url)
            if topic_id:
                try:
                    async with aiohttp.ClientSession(headers=self.headers) as s:
                        async with s.get(
                            f"{self.API}/topic/{topic_id}/comiclist?page=1&count=500",
                            timeout=aiohttp.ClientTimeout(total=15)
                        ) as r:
                            if r.status == 200:
                                data = await r.json()
                                clist = data.get("data", {}).get("comiclist", []) or data.get("data", {}).get("comics", [])
                                if clist:
                                    chapters = {}
                                    current_main_num = 0.0
                                    for i, ch in enumerate(clist):
                                        ch_id = ch.get("id")
                                        title = ch.get("title", "")
                                        locked = not ch.get("is_free", True)
                                        
                                        is_notice = any(k in title for k in ["公告", "停更", "请假", "休刊", "活动", "重磅上线"])
                                        is_extra = any(k in title for k in ["特别篇", "番外", "外传", "预告", "序章", "前传", "后记"])
                                        
                                        m_num = self.extract_chapter_number(title)
                                        if m_num is not None and not is_notice:
                                            if is_extra:
                                                num = current_main_num + 0.5 if current_main_num > 0 else float(m_num)
                                            else:
                                                num = float(m_num)
                                                current_main_num = num
                                        elif is_extra:
                                            num = current_main_num + 0.5 if current_main_num > 0 else float(i + 1)
                                        elif is_notice:
                                            continue
                                        else:
                                            current_main_num += 1.0
                                            num = current_main_num
                                            
                                        while num in chapters:
                                            num = round(num + 0.01, 2)
                                            
                                        ch_url = f"{self.BASE}/web/comic/{ch_id}"
                                        if ch_id:
                                            chapters[num] = {
                                                "url": ch_url,
                                                "locked": locked,
                                                "reason": "kuaikan-api",
                                            }
                                    return chapters
                except Exception as e:
                    print(f"[KuaikanLock] REST API attempt failed: {e}")

            # 2. Fallback to HTML scraping window.__NUXT__
            html = self.fetch_html(series_url)
            if not html:
                return {}
                
            soup = BeautifulSoup(html, "html.parser")
            nuxt_code = None
            for script in soup.find_all("script"):
                if script.string and "window.__NUXT__" in script.string:
                    nuxt_script = script.string.strip()
                    eq_idx = nuxt_script.find("=")
                    if eq_idx != -1:
                        nuxt_code = nuxt_script[eq_idx + 1 :].strip()
                        if nuxt_code.endswith(";"):
                            nuxt_code = nuxt_code[:-1].strip()
                    break

            if not nuxt_code:
                # Try BeautifulSoup generic selection as final fallback
                chapters = {}
                for a in soup.select("a[href*='/web/comic/']"):
                    href = a.get("href", "")
                    if not href.startswith("http"):
                        href = urljoin(self.BASE, href)
                    m_cid = re.search(r'/web/comic/(\d+)', href)
                    txt = a.get_text(strip=True)
                    nm = self.extract_chapter_number(txt)
                    if m_cid and nm is not None:
                        chapters[nm] = {
                            "url": href,
                            "locked": False,
                            "reason": "kuaikan-html-fallback",
                        }
                return chapters

            nuxt_data = self._parse_nuxt(nuxt_code)
            if not nuxt_data:
                return {}

            comics = []
            try:
                data_list = nuxt_data.get("data", [])
                if data_list:
                    res_data = data_list[0].get("res", {}).get("data", {})
                    topic_info = res_data.get("topic_info", {})
                    comics = topic_info.get("comics", [])
            except Exception:
                pass

            chapters = {}
            current_main_num = 0.0
            for i, ch in enumerate(comics):
                ch_id = ch.get("id")
                title = ch.get("title", "")
                is_free = ch.get("is_free")
                locked = not bool(is_free)
                
                is_notice = any(k in title for k in ["公告", "停更", "请假", "休刊", "活动", "重磅上线"])
                is_extra = any(k in title for k in ["特别篇", "番外", "外传", "预告", "序章", "前传", "后记"])
                
                m_num = self.extract_chapter_number(title)
                if m_num is not None and not is_notice:
                    if is_extra:
                        num = current_main_num + 0.5 if current_main_num > 0 else float(m_num)
                    else:
                        num = float(m_num)
                        current_main_num = num
                elif is_extra:
                    num = current_main_num + 0.5 if current_main_num > 0 else float(i + 1)
                elif is_notice:
                    continue
                else:
                    current_main_num += 1.0
                    num = current_main_num
                    
                while num in chapters:
                    num = round(num + 0.01, 2)
                    
                ch_url = f"{self.BASE}/web/comic/{ch_id}"
                if ch_id:
                    chapters[num] = {
                        "url": ch_url,
                        "title": title,
                        "locked": locked,
                        "reason": "kuaikan-nuxt",
                    }
            return chapters
        except Exception as e:
            print(f"[Kuaikan] get_chapters_with_lock_info error: {e}")
            return {}

    def _parse_nuxt(self, nuxt_code: str) -> dict:
        """
        Parses window.__NUXT__ minified JS expression.
        Uses node as primary parser, falls back to pure Python token parser.
        """
        import subprocess
        import tempfile
        import os

        # Try Node.js first
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".js", encoding="utf-8", delete=False) as f:
                temp_js = f.name
                f.write(f"const data = {nuxt_code}; console.log(JSON.stringify(data));")
            try:
                res = subprocess.run(["node", temp_js], capture_output=True, text=True, encoding="utf-8", timeout=5)
                if res.returncode == 0:
                    return json.loads(res.stdout)
            finally:
                if os.path.exists(temp_js):
                    os.remove(temp_js)
        except Exception as e:
            print(f"[Kuaikan] Node.js parser exception: {e}")

        # Python fallback parser (implements the matching brace and alias resolver)
        try:
            # Parse parameters
            param_match = re.search(r"^\(?function\(([^)]+)\)", nuxt_code)
            if not param_match:
                return {}
            params = [p.strip() for p in param_match.group(1).split(",")]

            # Find function body's matching brace
            fn_start = nuxt_code.find("{")
            brace_count = 1
            idx = fn_start + 1
            in_string = False
            quote_char = None
            escaped = False

            while idx < len(nuxt_code):
                c = nuxt_code[idx]
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif in_string:
                    if c == quote_char:
                        in_string = False
                else:
                    if c in ['"', "'"]:
                        in_string = True
                        quote_char = c
                    elif c == "{":
                        brace_count += 1
                    elif c == "}":
                        brace_count -= 1
                        if brace_count == 0:
                            break
                idx += 1

            body_str = nuxt_code[fn_start : idx + 1]

            # Extract args string
            args_str = nuxt_code[idx + 1 :].strip()
            if args_str.startswith("("):
                args_str = args_str[1:]
            elif args_str.startswith(")("):
                args_str = args_str[2:]

            while args_str.endswith(";") or args_str.endswith(")"):
                args_str = args_str[:-1].strip()

            # Parse args list using nesting-aware tokenizer
            def parse_js_array_nesting(s):
                tokens = []
                current = []
                in_string = False
                quote_char = None
                escaped = False
                array_depth = 0
                object_depth = 0
                paren_depth = 0

                s_idx = 0
                while s_idx < len(s):
                    c = s[s_idx]
                    if escaped:
                        current.append(c)
                        escaped = False
                    elif c == "\\":
                        current.append(c)
                        escaped = True
                    elif in_string:
                        current.append(c)
                        if c == quote_char:
                            in_string = False
                    else:
                        if c in ['"', "'"]:
                            in_string = True
                            quote_char = c
                            current.append(c)
                        elif c == "[":
                            array_depth += 1
                            current.append(c)
                        elif c == "]":
                            array_depth -= 1
                            current.append(c)
                        elif c == "{":
                            object_depth += 1
                            current.append(c)
                        elif c == "}":
                            object_depth -= 1
                            current.append(c)
                        elif c == "(":
                            paren_depth += 1
                            current.append(c)
                        elif c == ")":
                            paren_depth -= 1
                            current.append(c)
                        elif c == "," and array_depth == 0 and object_depth == 0 and paren_depth == 0:
                            val_str = "".join(current).strip()
                            tokens.append(val_str)
                            current = []
                        else:
                            current.append(c)
                    s_idx += 1
                val_str = "".join(current).strip()
                tokens.append(val_str)
                return tokens

            tokens = parse_js_array_nesting(args_str)

            # Convert tokens to python types
            args = []
            for t in tokens:
                if not t:
                    args.append(None)
                elif t.startswith('"') or t.startswith("'"):
                    args.append(t[1:-1])
                elif t == "true":
                    args.append(True)
                elif t == "false":
                    args.append(False)
                elif t in ["null", "undefined"]:
                    args.append(None)
                else:
                    try:
                        if "." in t:
                            args.append(float(t))
                        else:
                            args.append(int(t))
                    except ValueError:
                        args.append(t)

            if len(args) < len(params):
                args = args + [None] * (len(params) - len(args))

            aliases = dict(zip(params, args))

            # Find the return expression inside body_str (excluding the outer function body braces)
            body_content = body_str[1:-1].strip()
            return_idx = body_content.find("return")
            if return_idx == -1:
                return {}

            return_body = body_content[return_idx + 6 :].strip()
            if return_body.endswith(";"):
                return_body = return_body[:-1].strip()

            # Replace variables
            def replace_js_variables(js_obj_str, aliases):
                in_string = False
                quote_char = None
                escaped = False
                result = []
                current_word = []
                r_idx = 0
                while r_idx < len(js_obj_str):
                    c = js_obj_str[r_idx]
                    if escaped:
                        result.append(c)
                        escaped = False
                        r_idx += 1
                        continue
                    if c == "\\":
                        result.append(c)
                        escaped = True
                        r_idx += 1
                        continue
                    if in_string:
                        result.append(c)
                        if c == quote_char:
                            in_string = False
                        r_idx += 1
                        continue
                    if c in ['"', "'"]:
                        in_string = True
                        quote_char = c
                        result.append(c)
                        r_idx += 1
                        continue
                    if c.isalnum() or c in ["_", "$"]:
                        current_word.append(c)
                    else:
                        if current_word:
                            word = "".join(current_word)
                            next_chars = js_obj_str[r_idx:].strip()
                            is_current_key = next_chars.startswith(":")
                            if word in aliases and not is_current_key:
                                val = aliases[word]
                                if val is True:
                                    result.append("true")
                                elif val is False:
                                    result.append("false")
                                elif val is None:
                                    result.append("null")
                                elif isinstance(val, (int, float)):
                                    result.append(str(val))
                                else:
                                    result.append(json.dumps(val))
                            else:
                                result.append(word)
                            current_word = []
                        result.append(c)
                    r_idx += 1
                if current_word:
                    result.append("".join(current_word))
                return "".join(result)

            resolved_js = replace_js_variables(return_body, aliases)

            # Convert to JSON format by quoting keys
            json_str = re.sub(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', resolved_js)
            json_str = re.sub(r',\s*([\]}])', r'\1', json_str)

            return json.loads(json_str)
        except Exception as e:
            print(f"[Kuaikan] Python fallback parser exception: {e}")
            return {}

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            lock_info = await self.get_chapters_with_lock_info(series_url)
            return {num: item["url"] for num, item in lock_info.items()}
        except Exception as e:
            print(f"[Kuaikan] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        r    = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(r.keys()) if r else None


# ─────────────────────────────────────────────────────────────────
#  LINE Manga — manga.line.me
# ─────────────────────────────────────────────────────────────────
class LineMangaProvider(BaseProvider):
    """
    مزود LINE Manga (manga.line.me) — الفصول المجانية.
    """

    BASE = "https://manga.line.me"
    API  = "https://manga.line.me/a"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer":       "https://manga.line.me/",
            "Accept-Language": "ja-JP,ja;q=0.9",
        })

    def _extract_product_id(self, url: str) -> str:
        m = re.search(r'/product/(\d+)', url)
        return m.group(1) if m else None

    def _extract_chapter_id(self, url: str) -> str:
        m = re.search(r'/viewer/(\d+)', url)
        if not m:
            m = re.search(r'/chapter/(\d+)', url)
        return m.group(1) if m else None

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url)
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")

            # طريقة 1: inline JSON
            for script in soup.find_all("script"):
                content = script.string or ""
                if "contentUrl" in content or "imageUrl" in content:
                    imgs = re.findall(
                        r'"(?:contentUrl|imageUrl|src)"\s*:\s*"(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
                        content
                    )
                    if imgs:
                        return [i.replace("\\u002F", "/") for i in imgs]

            # طريقة 2: img مباشر
            images = []
            for img in soup.select("img[src*='manga.line.me'], img[src*='linem.jp']"):
                src = img.get("src") or img.get("data-src") or ""
                if src.startswith("http") and src not in images:
                    images.append(src)
            return images
        except Exception as e:
            print(f"[LINEManga] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            product_id = self._extract_product_id(series_url)
            if not product_id:
                return {}

            html = self.fetch_html(series_url)
            if not html:
                return {}
            soup     = BeautifulSoup(html, "html.parser")
            chapters = {}

            for a in soup.select("a[href*='/viewer/'], a[href*='/chapter/']"):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = urljoin(self.BASE, href)
                txt = a.get_text(strip=True)
                nm  = self.extract_chapter_number(txt)
                m   = re.search(r'/(?:viewer|chapter)/(\d+)', href)
                if m:
                    num = nm if nm is not None else float(m.group(1))
                    chapters[num] = href
            return chapters
        except Exception as e:
            print(f"[LINEManga] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        r    = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(r.keys()) if r else None


# ─────────────────────────────────────────────────────────────────
#  Piccoma — piccoma.com / piccoma.jp
# ─────────────────────────────────────────────────────────────────
class PiccomaProvider(BaseProvider):
    """
    مزود Piccoma (piccoma.com / piccoma.jp) — المحتوى المجاني الأسبوعي.
    """

    def __init__(self):
        super().__init__()

    def _base(self, url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    def _extract_ids(self, url: str):
        m = re.search(r'/product/(\d+).*?episode/(\d+)', url)
        if m:
            return m.group(1), m.group(2)
        m = re.search(r'/product/(\d+)', url)
        return (m.group(1), None) if m else (None, None)

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url)
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")

            # طريقة 1: NEXT_DATA
            nd = soup.find("script", id="__NEXT_DATA__")
            if nd:
                try:
                    text = json.dumps(json.loads(nd.string))
                    imgs = re.findall(
                        r'"(https?://[^"]+piccoma[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
                        text
                    )
                    if imgs:
                        return [i.replace("\\u002F", "/") for i in imgs]
                except Exception:
                    pass

            # طريقة 2: JavaScript inline
            for script in soup.find_all("script"):
                content = script.string or ""
                if "pageImageList" in content or "imageUrls" in content:
                    imgs = re.findall(
                        r'"(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', content
                    )
                    clean = [i for i in imgs if "piccoma" in i or "p-cdn" in i]
                    if clean:
                        return clean

            # طريقة 3: img tags
            images = []
            for img in soup.select("img[src*='piccoma'], img[src*='p-cdn']"):
                src = img.get("src") or img.get("data-src") or ""
                if src.startswith("http") and src not in images:
                    images.append(src)
            return images
        except Exception as e:
            print(f"[Piccoma] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url)
            if not html:
                return {}
            soup     = BeautifulSoup(html, "html.parser")
            base     = self._base(series_url)
            chapters = {}

            for a in soup.select("a[href*='/episode/'], a[href*='/viewer/']"):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = urljoin(base, href)
                txt = a.get_text(strip=True)
                nm  = self.extract_chapter_number(txt)
                m   = re.search(r'/episode/(\d+)', href)
                if m:
                    num = nm if nm is not None else float(m.group(1))
                    chapters[num] = href
            return chapters
        except Exception as e:
            print(f"[Piccoma] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        r    = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(r.keys()) if r else None


# ─────────────────────────────────────────────────────────────────
#  iQiyi Manhua — manhua.iqiyi.com
# ─────────────────────────────────────────────────────────────────
class IqiyiProvider(BaseProvider):
    """
    مزود iQiyi Manhua (manhua.iqiyi.com) — كوميكس iQiyi الصينية.
    """

    BASE = "https://manhua.iqiyi.com"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer":       "https://manhua.iqiyi.com/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url)
            if not html:
                return []
            # iQiyi يضع الصور في window.__initData__
            m = re.search(r'window\.__initData__\s*=\s*({.+?});\s*(?:window|var)', html, re.S)
            if m:
                try:
                    data = json.loads(m.group(1))
                    text = json.dumps(data)
                    imgs = re.findall(
                        r'"(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', text
                    )
                    clean = [i for i in imgs if "qpic" in i or "iqiyi" in i]
                    if clean:
                        return clean
                except Exception:
                    pass

            imgs = re.findall(
                r'"(https?://[^"]+(?:qpic|iqiyi)[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', html
            )
            return list(dict.fromkeys(imgs))
        except Exception as e:
            print(f"[iQiyi] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url)
            if not html:
                return {}
            soup     = BeautifulSoup(html, "html.parser")
            chapters = {}
            for a in soup.select("a[href*='/manhua/']"):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = urljoin(self.BASE, href)
                txt = a.get_text(strip=True)
                nm  = self.extract_chapter_number(txt)
                m   = re.search(r'/(\d+)(?:/|\.html)', href)
                if m and nm is not None:
                    chapters[nm] = href
            return chapters
        except Exception as e:
            print(f"[iQiyi] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        r    = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(r.keys()) if r else None


# ─────────────────────────────────────────────────────────────────
#  Lezhin — lezhin.com
# ─────────────────────────────────────────────────────────────────
class LezhinProvider(BaseProvider):
    """
    مزود Lezhin (lezhin.com) — الفصول المجانية والمحتوى المتاح للعامة.
    """

    BASE = "https://www.lezhin.com"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer":       "https://www.lezhin.com/",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        })

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url)
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")

            # 1. NEXT_DATA
            nd = soup.find("script", id="__NEXT_DATA__")
            if nd:
                try:
                    data = json.loads(nd.string)
                    text = json.dumps(data)
                    imgs = re.findall(
                        r'"(https?://[^"]+(?:lezhin|lz-cdn)[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
                        text
                    )
                    if imgs:
                        return list(dict.fromkeys([i.replace("\\u002F", "/") for i in imgs]))
                except Exception:
                    pass

            # 2. img tags
            images = []
            for img in soup.select("img[src*='lezhin'], img[src*='lz-cdn']"):
                src = img.get("src") or img.get("data-src") or ""
                if src.startswith("http") and src not in images:
                    images.append(src)
            return images
        except Exception as e:
            print(f"[Lezhin] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url)
            if not html:
                return {}
            soup     = BeautifulSoup(html, "html.parser")
            chapters = {}
            for a in soup.select("a[href*='/comic/']"):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = urljoin(self.BASE, href)
                parts = href.split("?")[0].rstrip("/").split("/")
                if len(parts) >= 6: # e.g. https://www.lezhin.com/ko/comic/slug/ep_id
                    ch_id = parts[-1]
                    txt   = a.get_text(strip=True)
                    nm    = self.extract_chapter_number(txt) or self.extract_chapter_number(ch_id)
                    if nm is not None:
                        chapters[nm] = href
            return chapters
        except Exception as e:
            print(f"[Lezhin] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        r    = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(r.keys()) if r else None


# ─────────────────────────────────────────────────────────────────
#  Toptoon — toptoon.com
# ─────────────────────────────────────────────────────────────────
class ToptoonProvider(BaseProvider):
    """
    مزود Toptoon (toptoon.com) — الفصول المجانية والمحتوى المتاح للعامة.
    """

    BASE = "https://toptoon.com"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer":       "https://toptoon.com/",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        })

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url)
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")
            images = []
            for img in soup.select("img[src*='toptoon'], img[src*='toptooncdn']"):
                src = img.get("src") or img.get("data-src") or ""
                if src.startswith("http") and src not in images:
                    images.append(src)
            return images
        except Exception as e:
            print(f"[Toptoon] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url)
            if not html:
                return {}
            soup     = BeautifulSoup(html, "html.parser")
            chapters = {}
            for a in soup.select("a[href*='ep_view']"):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = urljoin(self.BASE, href)
                txt = a.get_text(strip=True)
                nm  = self.extract_chapter_number(txt)
                if nm is not None:
                    chapters[nm] = href
            return chapters
        except Exception as e:
            print(f"[Toptoon] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        r    = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(r.keys()) if r else None


# ─────────────────────────────────────────────────────────────────
#  Ridibooks — ridibooks.com
# ─────────────────────────────────────────────────────────────────
class RidibooksProvider(BaseProvider):
    """
    مزود Ridibooks (ridibooks.com) — الفصول المجانية والمحتوى المتاح للعامة.
    """

    BASE = "https://ridibooks.com"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer":       "https://ridibooks.com/",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        })

    async def get_images(self, url: str) -> list[str]:
        try:
            m = re.search(r"/books/(\d+)", url)
            if not m:
                return []
            book_id = m.group(1)
            
            headers = {
                "Origin": "https://ridibooks.com",
                "Referer": f"https://ridibooks.com/books/{book_id}/view",
            }
            
            api_url = "https://ridibooks.com/api/web-viewer/generate"
            
            # fetch_json is synchronous, call in executor if running within async loop to avoid blocking
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(
                None,
                self.fetch_json,
                api_url,
                "POST",
                None,
                {"book_id": book_id},
                20,
                headers
            )
            
            if res and res.get("success"):
                pages = res.get("data", {}).get("pages", [])
                return [p["src"] for p in pages if "src" in p]
            return []
        except Exception as e:
            print(f"[Ridibooks] get_images error: {e}")
            return []

    async def get_chapters_with_lock_info(self, series_url: str) -> dict:
        try:
            loop = asyncio.get_event_loop()
            html = await loop.run_in_executor(None, self.fetch_html, series_url)
            if not html:
                return {}
            
            soup = BeautifulSoup(html, "html.parser")
            rows = soup.select(".book_list_wrapper")
            
            chapters = {}
            
            for idx, row in enumerate(rows):
                input_el = row.select_one("input.js_book_checkbox_input")
                book_id = input_el.get("value") if input_el else None
                if not book_id:
                    continue
                
                title_el = row.select_one(".js_book_title")
                title = title_el.get_text(strip=True) if title_el else f"Chapter {book_id}"
                
                has_free_badge = row.select_one(".free_badge") is not None
                has_direct_view = row.select_one(".serial_book_direct_view_button") is not None or row.select_one("a[href$='/view']") is not None
                
                locked = True
                reason = "requires_purchase"
                
                if has_free_badge or has_direct_view:
                    locked = False
                    reason = "free_access"
                
                # Extract chapter number
                m = re.search(r'(\d+(?:\.\d+)?)\s*(?:화|권)', title)
                ch_num = float(m.group(1)) if m else float(idx + 1)
                
                chapters[ch_num] = {
                    "url": f"https://ridibooks.com/books/{book_id}/view",
                    "locked": locked,
                    "reason": reason
                }
            return chapters
        except Exception as e:
            print(f"[Ridibooks] get_chapters_with_lock_info error: {e}")
            return {}

    async def get_all_chapters(self, series_url: str) -> dict:
        res = await self.get_chapters_with_lock_info(series_url)
        return {num: info["url"] for num, info in res.items()}

    async def get_latest_chapter(self, url: str):
        r = await self.get_all_chapters(url)
        return max(r.keys()) if r else None


# ─────────────────────────────────────────────────────────────────
#  Comico — comico.jp
# ─────────────────────────────────────────────────────────────────
class ComicoProvider(BaseProvider):
    """
    مزود Comico (comico.jp / comico.kr) — الفصول المجانية والمحتوى المتاح للعامة.
    """

    BASE = "https://www.comico.jp"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer":       "https://www.comico.jp/",
            "Accept-Language": "ja-JP,ja;q=0.9,ko-KR;q=0.8,en;q=0.7",
        })

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url)
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")
            images = []
            for img in soup.select("img[src*='comico']"):
                src = img.get("src") or img.get("data-src") or ""
                if src.startswith("http") and src not in images:
                    images.append(src)
            return images
        except Exception as e:
            print(f"[Comico] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url)
            if not html:
                return {}
            soup     = BeautifulSoup(html, "html.parser")
            chapters = {}
            for a in soup.select("a[href*='/comic/'], a[href*='/title/'], a[href*='/viewer/']"):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = urljoin(self.BASE, href)
                parts = href.split("?")[0].rstrip("/").split("/")
                if len(parts) >= 5:
                    ch_num_str = parts[-1]
                    nm = self.extract_chapter_number(ch_num_str)
                    if nm is None:
                        txt = a.get_text(strip=True)
                        nm  = self.extract_chapter_number(txt)
                    if nm is not None:
                        chapters[nm] = href
            return chapters
        except Exception as e:
            print(f"[Comico] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        r    = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(r.keys()) if r else None


# ─────────────────────────────────────────────────────────────────
#  Jumptoon — jumptoon.com
# ─────────────────────────────────────────────────────────────────
class JumptoonProvider(BaseProvider):
    """
    مزود Jumptoon (jumptoon.com) — الفصول المجانية والمحتوى المتاح للعامة.
    """

    BASE = "https://jumptoon.com"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer":       "https://jumptoon.com/",
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
        })

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url)
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")
            images = []
            for img in soup.select("img[src*='jumptoon']"):
                src = img.get("src") or img.get("data-src") or ""
                if src.startswith("http") and src not in images:
                    images.append(src)
            return images
        except Exception as e:
            print(f"[Jumptoon] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url)
            if not html:
                return {}
            soup     = BeautifulSoup(html, "html.parser")
            chapters = {}
            for a in soup.select("a[href*='/chapters/']"):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = urljoin(self.BASE, href)
                parts = href.split("?")[0].rstrip("/").split("/")
                if len(parts) >= 6:
                    ch_num_str = parts[-1]
                    nm = self.extract_chapter_number(ch_num_str)
                    if nm is None:
                        txt = a.get_text(strip=True)
                        nm  = self.extract_chapter_number(txt)
                    if nm is not None:
                        chapters[nm] = href
            return chapters
        except Exception as e:
            print(f"[Jumptoon] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        r    = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(r.keys()) if r else None


# ─────────────────────────────────────────────────────────────────
#  Mechacomic — mechacomic.jp
# ─────────────────────────────────────────────────────────────────
class MechacomicProvider(BaseProvider):
    """
    مزود Mechacomic (mechacomic.jp) — الفصول المجانية والمحتوى المتاح للعامة.
    """

    BASE = "https://mechacomic.jp"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer":       "https://mechacomic.jp/",
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
        })

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url)
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")
            images = []
            for img in soup.select("img[src*='mechacomic'], img[src*='mecha']"):
                src = img.get("src") or img.get("data-src") or ""
                if src.startswith("http") and src not in images:
                    images.append(src)
            return images
        except Exception as e:
            print(f"[Mechacomic] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url)
            if not html:
                return {}
            soup     = BeautifulSoup(html, "html.parser")
            chapters = {}
            for a in soup.select("a[href*='/books/']"):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = urljoin(self.BASE, href)
                parts = href.split("?")[0].rstrip("/").split("/")
                if len(parts) >= 5:
                    ch_num_str = parts[-1]
                    nm = self.extract_chapter_number(ch_num_str)
                    if nm is None:
                        txt = a.get_text(strip=True)
                        nm  = self.extract_chapter_number(txt)
                    if nm is not None:
                        chapters[nm] = href
            return chapters
        except Exception as e:
            print(f"[Mechacomic] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        r    = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(r.keys()) if r else None


# ─────────────────────────────────────────────────────────────────
#  Munpia — munpia.com
# ─────────────────────────────────────────────────────────────────
class MunpiaProvider(BaseProvider):
    """
    مزود Munpia (munpia.com) — الفصول المجانية والمحتوى المتاح للعامة.
    """

    BASE = "https://www.munpia.com"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer":       "https://www.munpia.com/",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        })

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url)
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")
            images = []
            for img in soup.select("img[src*='munpia']"):
                src = img.get("src") or img.get("data-src") or ""
                if src.startswith("http") and src not in images:
                    images.append(src)
            return images
        except Exception as e:
            print(f"[Munpia] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url)
            if not html:
                return {}
            soup     = BeautifulSoup(html, "html.parser")
            chapters = {}
            for a in soup.select("a[href*='/page/'], a[href*='/viewer/']"):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = urljoin(self.BASE, href)
                parts = href.split("?")[0].rstrip("/").split("/")
                if len(parts) >= 5:
                    ch_num_str = parts[-1]
                    nm = self.extract_chapter_number(ch_num_str)
                    if nm is None:
                        txt = a.get_text(strip=True)
                        nm  = self.extract_chapter_number(txt)
                    if nm is not None:
                        chapters[nm] = href
            return chapters
        except Exception as e:
            print(f"[Munpia] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        r    = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(r.keys()) if r else None


# ─────────────────────────────────────────────────────────────────
#  MrBlue — (mrblue.com)
# ─────────────────────────────────────────────────────────────────
class MrblueProvider(BaseProvider):
    BASE = "https://www.mrblue.com"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer": "https://www.mrblue.com/",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        })

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url)
            if not html and hasattr(self, "playwright") and self.playwright:
                html = await self.playwright.fetch_html_playwright(url)
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")
            images = []
            for img in soup.select("img[src*='mrblue'], img[data-src*='mrblue']"):
                src = img.get("src") or img.get("data-src") or ""
                if src.startswith("http") and src not in images:
                    images.append(src)
            return images
        except Exception as e:
            print(f"[MrBlue] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url)
            if not html and hasattr(self, "playwright") and self.playwright:
                html = await self.playwright.fetch_html_playwright(series_url)
            if not html:
                return {}
            soup = BeautifulSoup(html, "html.parser")
            chapters = {}
            for a in soup.select("a[href*='/novel/'], a[href*='/webtoon/'], a[href*='/comic/']"):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = urljoin(self.BASE, href)
                nm = self.extract_chapter_number(a.get_text(strip=True))
                if nm is not None and nm not in chapters:
                    chapters[nm] = href
            return chapters
        except Exception as e:
            print(f"[MrBlue] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        r = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(r.keys()) if r else None


# ─────────────────────────────────────────────────────────────────
#  Manta — (manta.net)
# ─────────────────────────────────────────────────────────────────
class MantaProvider(BaseProvider):
    BASE = "https://manta.net"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer": "https://manta.net/",
            "Accept-Language": "en-US,en;q=0.9",
        })

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url)
            if not html and hasattr(self, "playwright") and self.playwright:
                html = await self.playwright.fetch_html_playwright(url)
            if not html:
                return []
            images = re.findall(r'"url"\s*:\s*"(https?://[^"]+manta[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', html, re.I)
            if not images:
                soup = BeautifulSoup(html, "html.parser")
                images = [img.get("src") or img.get("data-src") for img in soup.select("img") if img.get("src") or img.get("data-src")]
            return [img.replace("\\u002F", "/") for img in images if img and img.startswith("http")]
        except Exception as e:
            print(f"[Manta] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url)
            if not html and hasattr(self, "playwright") and self.playwright:
                html = await self.playwright.fetch_html_playwright(series_url)
            if not html:
                return {}
            soup = BeautifulSoup(html, "html.parser")
            chapters = {}
            for a in soup.select("a[href*='/episode/'], a[href*='/series/']"):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = urljoin(self.BASE, href)
                nm = self.extract_chapter_number(a.get_text(strip=True))
                if nm is not None and nm not in chapters:
                    chapters[nm] = href
            return chapters
        except Exception as e:
            print(f"[Manta] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        r = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(r.keys()) if r else None


# ─────────────────────────────────────────────────────────────────
#  BomToon — (bomtoon.com)
# ─────────────────────────────────────────────────────────────────
class BomtoonProvider(BaseProvider):
    BASE = "https://www.bomtoon.com"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer": "https://www.bomtoon.com/",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        })

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url)
            if not html and hasattr(self, "playwright") and self.playwright:
                html = await self.playwright.fetch_html_playwright(url)
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")
            images = []
            for img in soup.select("img[src*='bomtoon'], img[data-src*='bomtoon']"):
                src = img.get("src") or img.get("data-src") or ""
                if src.startswith("http") and src not in images:
                    images.append(src)
            return images
        except Exception as e:
            print(f"[Bomtoon] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url)
            if not html and hasattr(self, "playwright") and self.playwright:
                html = await self.playwright.fetch_html_playwright(series_url)
            if not html:
                return {}
            soup = BeautifulSoup(html, "html.parser")
            chapters = {}
            for a in soup.select("a[href*='/comic/ep_list/'], a[href*='/viewer/']"):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = urljoin(self.BASE, href)
                nm = self.extract_chapter_number(a.get_text(strip=True))
                if nm is not None and nm not in chapters:
                    chapters[nm] = href
            return chapters
        except Exception as e:
            print(f"[Bomtoon] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        r = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(r.keys()) if r else None


# ─────────────────────────────────────────────────────────────────
#  Manga UP! — (manga-up.com)
# ─────────────────────────────────────────────────────────────────
class MangaupProvider(BaseProvider):
    BASE = "https://www.manga-up.com"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer": "https://www.manga-up.com/",
            "Accept-Language": "en-US,en;q=0.9",
        })

    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url)
            if not html and hasattr(self, "playwright") and self.playwright:
                html = await self.playwright.fetch_html_playwright(url)
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")
            images = []
            for img in soup.select("img"):
                src = img.get("src") or img.get("data-src") or ""
                if src.startswith("http") and ("manga-up" in src or "mangaup" in src or "square-enix" in src) and src not in images:
                    images.append(src)
            return images
        except Exception as e:
            print(f"[MangaUP] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url)
            if not html and hasattr(self, "playwright") and self.playwright:
                html = await self.playwright.fetch_html_playwright(series_url)
            if not html:
                return {}
            soup = BeautifulSoup(html, "html.parser")
            chapters = {}
            for a in soup.select("a[href*='/title/'], a[href*='/chapter/']"):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = urljoin(self.BASE, href)
                nm = self.extract_chapter_number(a.get_text(strip=True))
                if nm is not None and nm not in chapters:
                    chapters[nm] = href
            return chapters
        except Exception as e:
            print(f"[MangaUP] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        r = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(r.keys()) if r else None

