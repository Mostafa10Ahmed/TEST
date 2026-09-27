import re

with open('providers/base_provider.py', 'r', encoding='utf-8') as f:
    code = f.read()

new_func = '''
def fetch_with_curl(url: str, headers: dict = None, timeout: int = 25,
                    method: str = "GET", data=None) -> Optional[str]:
    if not CURL_AVAILABLE:
        return None
    h = {**CHROME_HEADERS, **(headers or {})}
    cookies = get_cookies_for_url(url)
    
    # Apply custom User-Agent if present in cookies
    if cookies and "__custom_user_agent" in cookies:
        h["User-Agent"] = cookies.pop("__custom_user_agent")
        
    # Format cookies as Cookie header just to be safe
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        h["Cookie"] = cookie_str
        
        # some sites expect auth as Bearer
        if "access_token" in cookies and "Authorization" not in h:
            h["Authorization"] = f"Bearer {cookies['access_token']}"
            
    for target in ["chrome131", "chrome124", "chrome120", "safari180"]:
        try:
            if method == "POST":
                resp = curl_requests.post(url, headers=h, data=data, cookies=cookies,
                                          timeout=timeout, impersonate=target)
            else:
                resp = curl_requests.get(url, headers=h, cookies=cookies, timeout=timeout,
                                         impersonate=target, allow_redirects=True)
            if resp.status_code == 200:
                return resp.text
        except Exception as e:
            if "not supported" not in str(e).lower():
                continue
    return None
'''

# Find the old fetch_with_curl function and replace it
pattern = r'def fetch_with_curl[\s\S]*?(?=class BaseProvider:)'
code = re.sub(pattern, new_func + '\n\n', code)

# Also update fetch_html in BaseProvider
fetch_html_func = '''
    def fetch_html(self, url: str, extra_headers: dict = None, timeout: int = 25) -> Optional[str]:
        h = {**self.headers, **(extra_headers or {})}
        cookies = get_cookies_for_url(url)
        
        if cookies and "__custom_user_agent" in cookies:
            h["User-Agent"] = cookies.pop("__custom_user_agent")
            
        if cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
            if "access_token" in cookies and "Authorization" not in h:
                h["Authorization"] = f"Bearer {cookies['access_token']}"
        
        html = fetch_with_curl(url, h, timeout)
        if html and len(html) > 500:
            return html
        try:
            resp = self.scraper.get(url, headers=h, cookies=cookies, timeout=timeout)
            if resp.status_code == 200:
                return resp.text
        except Exception as e:
            print(f"[cloudscraper] {url}: {e}")
        return None
'''

pattern2 = r'    def fetch_html[\s\S]*?(?=    def fetch_json)'
code = re.sub(pattern2, fetch_html_func + '\n', code)

with open('providers/base_provider.py', 'w', encoding='utf-8') as f:
    f.write(code)

print("Injected user-agent and cookie logic into base_provider.py")
