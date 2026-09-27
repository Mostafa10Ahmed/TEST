import re
from typing import Callable, List, Optional

import urllib3.util.connection as urllib3_cn
import socket

def allowed_gai_family():
    return socket.AF_INET

urllib3_cn.allowed_gai_family = allowed_gai_family

import cloudscraper
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as curl_requests

    CURL_AVAILABLE = True
except ImportError:
    CURL_AVAILABLE = False

USER_AGENT_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def get_random_user_agent() -> str:
    import random
    return random.choice(USER_AGENT_POOL)


def is_cloudflare_challenge(status_code: int = 200, html: str = "") -> bool:
    if status_code in (403, 503):
        low_html = (html or "").lower()
        if any(kw in low_html for kw in ["just a moment", "attention required", "cf-browser-verification", "cloudflare", "challenge-platform", "enable javascript"]):
            return True
    if html:
        low_html = html.lower()
        if "just a moment..." in low_html or "cf-browser-verification" in low_html or "<title>just a moment...</title>" in low_html:
            return True
    return False


def is_retryable_response(status_code: int = 200, html: str = "") -> bool:
    if status_code in (429, 502, 503):
        return True
    if is_cloudflare_challenge(status_code, html):
        return True
    return False


CHROME_HEADERS = {
    "User-Agent": USER_AGENT_POOL[0],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

AJAX_HEADERS = {
    **CHROME_HEADERS,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

# كاش عالمي لبيانات تسجيل الدخول (كوكيز) للمواقع
SITE_AUTH = {}


def update_site_auth_cache(auth_dict: dict):
    global SITE_AUTH
    SITE_AUTH.update(auth_dict)


def _is_token_expired(token: str) -> bool:
    """يتحقق هل الـ JWT منتهي الصلاحية"""
    try:
        import base64
        import json
        import time

        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        decoded = json.loads(base64.b64decode(payload))
        exp = decoded.get("exp", 0)
        # اعتبره منتهياً لو باقي له أقل من 5 دقائق
        return int(time.time()) >= (exp - 300)
    except Exception:
        return False


def _persist_site_auth(domain: str, auth_data: dict):
    """يحفظ كوكيز الموقع في الذاكرة وقاعدة البيانات وملفات الكاش للـ Worker."""
    global SITE_AUTH
    SITE_AUTH[domain] = auth_data

    # 1. تحديث قاعدة البيانات في بيئة البوت الأساسية
    try:
        import database
        import asyncio
        loop = None
        try:
            loop = asyncio.get_event_loop()
        except Exception:
            pass
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(database.set_site_auth(domain, auth_data), loop)
        else:
            asyncio.run(database.set_site_auth(domain, auth_data))
    except Exception:
        pass

    # 2. تحديث ملفات JSON في كلا البيئتين (البوت والـ Worker)
    try:
        import json
        import os
        paths = ["data/site_auth_cache.json", "site_auth_cache.json"]
        for p in paths:
            dir_name = os.path.dirname(p)
            if (dir_name and os.path.exists(dir_name)) or os.path.exists(p):
                existing = {}
                if os.path.exists(p):
                    with open(p, "r", encoding="utf-8") as f:
                        existing = json.load(f) or {}
                existing[domain] = auth_data
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _refresh_asura_token(domain: str) -> bool:
    """يجدد الـ access_token باستخدام refresh_token لأي دومين Asura"""
    global SITE_AUTH
    cookies = SITE_AUTH.get(domain, {})
    refresh_token = cookies.get("refresh_token")
    if not refresh_token:
        for k, v in SITE_AUTH.items():
            if "asura" in k and isinstance(v, dict) and v.get("refresh_token"):
                cookies = dict(v)
                refresh_token = v.get("refresh_token")
                break
    if not refresh_token:
        return False
    try:
        import json as _json

        ua = cookies.get("__custom_user_agent") or get_random_user_agent()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": ua,
            "Origin": "https://asurascans.com",
            "Referer": "https://asurascans.com/"
        }

        payload = _json.dumps({"refresh_token": refresh_token})
        raw = fetch_with_curl(
            "https://api.asurascans.com/api/auth/refresh",
            headers=headers,
            timeout=15,
            method="POST",
            data=payload
        )
        
        if not raw:
            import requests as _req
            r = _req.post(
                "https://api.asurascans.com/api/auth/refresh",
                json={"refresh_token": refresh_token},
                headers=headers,
                timeout=15,
            )
            if r.status_code == 200:
                raw = r.text

        if raw:
            res_json = _json.loads(raw)
            data = res_json.get("data", {}) if isinstance(res_json, dict) else {}
            new_token = data.get("access_token")
            new_refresh = data.get("refresh_token")
            if new_token:
                cookies["access_token"] = new_token
                if new_refresh:
                    cookies["refresh_token"] = new_refresh
                for asura_dom in ["asurascans.com", "asura.gg", "asuracomics.com", "asuratoon.com", "api.asurascans.com"]:
                    _persist_site_auth(asura_dom, cookies)
                print(f"[Auth] Token refreshed and persisted OK for all Asura domains")
                return True
    except Exception as e:
        print(f"[Auth] Token refresh failed for {domain}: {e}")
    return False


def _auth_domain_candidates(domain: str) -> list[str]:
    domain = domain.replace("www.", "").lower().strip(".")
    candidates = [domain]
    if domain.startswith("api."):
        candidates.append(domain[4:])
    parts = domain.split(".")
    for i in range(1, max(1, len(parts) - 1)):
        parent = ".".join(parts[i:])
        if parent not in candidates:
            candidates.append(parent)
    if "asura" in domain:
        for alias in (
            "asurascans.com",
            "api.asurascans.com",
            "asura.gg",
            "asuracomics.com",
            "asuratoon.com",
        ):
            if alias not in candidates:
                candidates.append(alias)
    return candidates


def get_cookies_for_url(url: str) -> dict:
    from urllib.parse import urlparse

    domain = urlparse(url).netloc.replace("www.", "").lower()
    selected_domain = domain
    cookies = {}
    for candidate in _auth_domain_candidates(domain):
        if SITE_AUTH.get(candidate):
            selected_domain = candidate
            cookies = SITE_AUTH.get(candidate, {}) or {}
            break

    # تجديد تلقائي للتوكن لو منتهي الصلاحية
    if cookies.get("access_token") and _is_token_expired(cookies["access_token"]):
        _refresh_asura_token(selected_domain)
        cookies = SITE_AUTH.get(selected_domain, {}) or {}
    return dict(cookies)


def create_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True},
        delay=3,
    )


def fetch_with_curl(
    url: str,
    headers: dict = None,
    timeout: int = 25,
    method: str = "GET",
    data=None,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    max_retries: int = 3,
) -> Optional[str]:
    if not CURL_AVAILABLE:
        return None
    import time
    h = {**CHROME_HEADERS, **(headers or {})}
    cookies = get_cookies_for_url(url)

    if cookies and "__custom_user_agent" in cookies:
        h["User-Agent"] = cookies.get("__custom_user_agent")
    elif "User-Agent" not in h or h["User-Agent"] == CHROME_HEADERS["User-Agent"]:
        h["User-Agent"] = get_random_user_agent()

    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items() if k != "__custom_user_agent")
        h["Cookie"] = cookie_str
        if "access_token" in cookies and "Authorization" not in h:
            h["Authorization"] = f"Bearer {cookies['access_token']}"

    targets = ["chrome124", "chrome131", "chrome120", "safari180"]
    for attempt in range(max_retries + 1):
        if attempt > 0:
            h["User-Agent"] = get_random_user_agent()
            delay = initial_delay * (backoff_factor ** (attempt - 1))
            time.sleep(delay)

        for target in targets:
            try:
                if method == "POST":
                    resp = curl_requests.post(
                        url,
                        headers=h,
                        data=data,
                        timeout=timeout,
                        impersonate=target,
                    )
                else:
                    resp = curl_requests.get(
                        url,
                        headers=h,
                        timeout=timeout,
                        impersonate=target,
                        allow_redirects=True,
                    )
                if resp.status_code == 200 and not is_cloudflare_challenge(resp.status_code, resp.text):
                    return resp.text
                if is_retryable_response(resp.status_code, resp.text):
                    break  # Retry next attempt loop with backoff
            except Exception as e:
                if "not supported" not in str(e).lower():
                    continue
    return None


class BaseProvider:
    def __init__(self, scraper=None):
        self.scraper = scraper or create_scraper()
        self.headers = CHROME_HEADERS.copy()

    def fetch_html(
        self,
        url: str,
        extra_headers: dict = None,
        timeout: int = 25,
        initial_delay: float = 1.0,
        backoff_factor: float = 2.0,
        max_retries: int = 3,
    ) -> Optional[str]:
        import time
        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = initial_delay * (backoff_factor ** (attempt - 1))
                time.sleep(delay)

            h = {**self.headers, **(extra_headers or {})}
            cookies = get_cookies_for_url(url)

            if cookies and "__custom_user_agent" in cookies:
                h["User-Agent"] = cookies.pop("__custom_user_agent")
            else:
                h["User-Agent"] = get_random_user_agent()

            if cookies:
                h["Cookie"] = "; ".join(
                    f"{k.strip()}={str(v).strip().replace(chr(10),'').replace(chr(13),'')}"
                    for k, v in cookies.items()
                )
                if "access_token" in cookies and "Authorization" not in h:
                    h["Authorization"] = f"Bearer {cookies['access_token'].strip()}"

            html = fetch_with_curl(url, h, timeout, initial_delay=0.1, max_retries=0)
            if html and len(html) > 500 and not is_cloudflare_challenge(200, html):
                return html

            status = 0
            try:
                resp = self.scraper.get(url, headers=h, cookies=cookies, timeout=timeout)
                status = resp.status_code
                if resp.status_code == 200 and len(resp.text) > 500 and not is_cloudflare_challenge(resp.status_code, resp.text):
                    return resp.text
            except Exception as e:
                print(f"[cloudscraper] {url}: {e}")

            # fallback to standard requests
            try:
                import requests as _req
                resp = _req.get(url, headers=h, cookies=cookies, timeout=timeout)
                status = resp.status_code
                if resp.status_code == 200 and len(resp.text) > 500 and not is_cloudflare_challenge(resp.status_code, resp.text):
                    return resp.text
            except Exception as e:
                print(f"[standard requests fallback] {url}: {e}")

            if not is_retryable_response(status, ""):
                break

        return None

    def fetch_json(
        self,
        url: str,
        method: str = "GET",
        data=None,
        json_data=None,
        timeout: int = 20,
        extra_headers: dict = None,
        initial_delay: float = 1.0,
        backoff_factor: float = 2.0,
        max_retries: int = 3,
    ) -> Optional[dict]:
        """جلب JSON من API endpoint مع دعم الكوكيز والـ auth headers والأعادة بباك أوف."""
        import json as _json
        import time

        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = initial_delay * (backoff_factor ** (attempt - 1))
                time.sleep(delay)

            h = {**AJAX_HEADERS, **(extra_headers or {})}
            cookies = get_cookies_for_url(url)

            if cookies and "__custom_user_agent" in cookies:
                h["User-Agent"] = cookies.pop("__custom_user_agent")
            else:
                h["User-Agent"] = get_random_user_agent()

            if cookies:
                h["Cookie"] = "; ".join(
                    f"{k.strip()}={str(v).strip().replace(chr(10),'').replace(chr(13),'')}"
                    for k, v in cookies.items()
                )
                if "access_token" in cookies and "Authorization" not in h:
                    h["Authorization"] = f"Bearer {cookies['access_token'].strip()}"

            status = 0
            try:
                if method.upper() == "POST":
                    resp = self.scraper.post(
                        url,
                        data=data,
                        json=json_data,
                        headers=h,
                        cookies=cookies,
                        timeout=timeout,
                    )
                else:
                    resp = self.scraper.get(
                        url, headers=h, cookies=cookies, timeout=timeout
                    )
                status = resp.status_code
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                pass

            # fallback curl_cffi
            try:
                curl_data = data
                if json_data is not None:
                    curl_data = _json.dumps(json_data)
                    h.setdefault("Content-Type", "application/json")
                raw = fetch_with_curl(
                    url, h, timeout, method=method.upper(), data=curl_data, initial_delay=0.1, max_retries=0
                )
                if raw:
                    return _json.loads(raw)
            except Exception:
                pass

            # fallback to standard requests
            try:
                import requests as _req
                curl_data = data
                if json_data is not None:
                    curl_data = _json.dumps(json_data)
                    h.setdefault("Content-Type", "application/json")
                if method.upper() == "POST":
                    resp = _req.post(url, headers=h, data=curl_data, cookies=cookies, timeout=timeout)
                else:
                    resp = _req.get(url, headers=h, cookies=cookies, timeout=timeout)
                status = resp.status_code
                if resp.status_code == 200:
                    return resp.json()
            except Exception as e:
                print(f"[standard requests json fallback] {url}: {e}")

            if not is_retryable_response(status, ""):
                break

        return None

    # ── Pagination helper ─────────────────────────────────────────────────
    def _paginate_chapters(
        self,
        base_url: str,
        extract_fn: Callable[[str, str], dict],
        max_pages: int = 40,
    ) -> dict:
        """
        يجرب أنماط Pagination متعددة ويجمع كل الفصول:
          • ?page=N
          • /page/N/
          • ?p=N
        يتوقف عندما لا تجد فصولاً جديدة في صفحة ما.
        """
        all_chapters: dict = {}

        patterns = [
            lambda u, n: f"{u.rstrip('/')}?page={n}",
            lambda u, n: f"{u.rstrip('/')}/page/{n}/",
            lambda u, n: f"{u.rstrip('/')}?p={n}",
        ]

        for pattern in patterns:
            found_any_new = False
            for page_num in range(2, max_pages + 1):
                try:
                    page_url = pattern(base_url, page_num)
                    html = self.fetch_html(page_url)
                    if not html or len(html) < 500:
                        break
                    new_chs = extract_fn(html, base_url)
                    if not new_chs:
                        break
                    before = len(all_chapters)
                    all_chapters.update(new_chs)
                    if len(all_chapters) == before:
                        break  # نفس الفصول = آخر صفحة
                    found_any_new = True
                except Exception:
                    break
            if found_any_new:
                break  # النمط الأول الذي نجح يكفي

        return all_chapters

    def _extract_chapter_links(self, html: str, base_url: str) -> dict:
        """استخراج روابط الفصول من HTML خام — مساعد مشترك"""
        from urllib.parse import urljoin, urlparse

        soup = BeautifulSoup(html, "html.parser")
        parsed = urlparse(base_url)
        domain = f"{parsed.scheme}://{parsed.netloc}"
        chs = {}
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("http"):
                href = urljoin(domain, href)
            m = re.search(r"(?:chapter[s]?|ch)[/-](\d+(?:\.\d+)?)", href, re.I)
            if m and domain.split("//")[1].split("/")[0] in href:
                try:
                    num = float(m.group(1))
                    if num not in chs:
                        chs[num] = href
                except Exception:
                    pass
        return chs

    def get_latest_chapter(self, url: str) -> Optional[float]:
        raise NotImplementedError

    def get_images(self, url: str) -> List[str]:
        raise NotImplementedError

    def get_all_chapters(self, url: str) -> dict:
        raise NotImplementedError

    def extract_chapter_number(self, text: str) -> Optional[float]:
        if not text:
            return None
        text = text.strip()

        # 1. Standard prefix pattern (case-insensitive)
        m = re.search(
            r"(?i)(?:الفصل|فصل|chapter|ch|ep|v|第)\s*[:\-]?\s*(\d+(?:\.\d+)?)", text
        )
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass

        # 2. Chinese chapter units prefix pattern: e.g. "60话", "60章", "60集"
        m = re.search(r'(\d+(?:\.\d+)?)\s*(?:话|章|集|回|册)', text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass

        # 3. Leading numbers (possibly in brackets like [60-...) followed by divider/space
        # e.g., "001-...", "[002] ...", "03: ...", "45 ...", "100_..."
        m = re.match(r'^\s*\[?\s*(\d+(?:\.\d+)?)\s*[-_\]\s:：.]', text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass

        # 4. Fallback: just any leading float/integer number
        m = re.match(r'^\s*(\d+(?:\.\d+)?)', text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass

        return None
