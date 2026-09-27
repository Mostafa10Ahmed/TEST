import asyncio
import logging
import time

import aiohttp
from bot_config import Config

try:
    import database as _database
except ImportError:
    _database = None  # type: ignore

logger = logging.getLogger("RemoteDownloader")


class RemoteDownloader:
    """
    يتصل بـ HuggingFace Worker لتنفيذ عمليات التحميل والدمج الثقيلة.
    """

    def __init__(self):
        self.base_url = (
            Config.HF_WORKER_URL.rstrip("/") if Config.HF_WORKER_URL else None
        )
        self.api_key = Config.HF_WORKER_KEY
        self._session: aiohttp.ClientSession | None = None
        self._health_cache: dict | None = None
        self._health_cache_ts: float = 0.0
        self._health_ttl_sec: float = 10.0

    @property
    def is_enabled(self):
        # لازم URL + secret، غير كذا راح يعطي 401/سلوك غامض
        return bool(self.base_url and self.api_key)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            connector = aiohttp.TCPConnector(
                limit=20, limit_per_host=10, enable_cleanup_closed=True
            )
            self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_headers(self) -> dict:
        import os
        headers = {}
        hf_token = os.getenv("HF_TOKEN")
        if not hf_token:
            try:
                from huggingface_hub import get_token
                hf_token = get_token()
            except Exception:
                pass
        if hf_token:
            headers["Authorization"] = f"Bearer {hf_token}"
        elif self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        if self.api_key:
            headers["X-Worker-Key"] = self.api_key
        return headers

    async def _post(self, endpoint, data):
        if not self.is_enabled:
            return {
                "error": "HF Worker not configured (HF_WORKER_URL/HF_WORKER_KEY missing)"
            }

        headers = self._get_headers()
        try:
            session = await self._get_session()
            async with session.post(
                f"{self.base_url}{endpoint}", json=data, headers=headers
            ) as resp:
                if resp.status != 200:
                    return {
                        "error": f"Worker returned status {resp.status}",
                        "details": await resp.text(),
                    }
                return await resp.json()
        except Exception as e:
            return {"error": f"Connection error: {str(e)}"}

    async def _get(self, endpoint):
        if not self.is_enabled:
            return {
                "error": "HF Worker not configured (HF_WORKER_URL/HF_WORKER_KEY missing)"
            }

        headers = self._get_headers()
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.base_url}{endpoint}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return {"error": f"Worker returned status {resp.status}"}
                return await resp.json()
        except Exception as e:
            return {"error": f"Connection error: {str(e)}"}

    async def create_remote_folder(self, folder_name: str, upload_dest: str):
        """يطلب من الـ Worker إنشاء مجلد لتجميع الفصول"""
        data = {"folder_name": folder_name, "upload_dest": upload_dest}
        return await self._post("/create_folder", data)

    async def _get_current_site_auth(self) -> dict:
        """يرجع auth للمواقع من قاعدة البيانات (للإرسال مع كل job)."""
        try:
            if _database:
                return await _database.get_all_site_auth_data()
        except Exception:
            pass
        return {}

    async def start_download(self, url, title, job_type="manga", params=None):
        """يرسل طلب بدء تحميل إلى الـ Worker مع auth المواقع."""
        p = dict(params or {})
        # أرسل auth مع كل job حتى لو لم يتم /sync_custom_data
        if not p.get("site_auth"):
            site_auth = await self._get_current_site_auth()
            if site_auth:
                p["site_auth"] = site_auth
        data = {
            "url": url,
            "title": title,
            "job_type": job_type,
            "params": p,
        }
        return await self._post("/jobs", data)

    async def start_stitch(self, drive_url, title, width, height, sensitivity):
        """يرسل طلب بدء دمج من Drive إلى الـ Worker"""
        data = {
            "url": drive_url,
            "title": title,
            "job_type": "stitch",
            "params": {"width": width, "height": height, "sensitivity": sensitivity},
        }
        return await self._post("/jobs", data)

    async def get_job_status(self, job_id):
        """يتحقق من حالة المهمة"""
        return await self._get(f"/jobs/{job_id}")

    async def get_all_jobs(self):
        """يسترجع كافة المهام من العامل"""
        return await self._get("/jobs")

    async def get_worker_health(self, force_refresh: bool = False):
        if not self.is_enabled:
            return {"error": "HF Worker URL not configured"}
        now = time.time()
        if (
            not force_refresh
            and self._health_cache
            and (now - self._health_cache_ts) < self._health_ttl_sec
        ):
            return self._health_cache
        res = await self._get("/health")
        if "error" not in res:
            self._health_cache = res
            self._health_cache_ts = now
        return res

    async def wait_for_job(
        self, job_id, progress_callback=None, max_wait_sec: int = 7200
    ):
        """ينتظر اكتمال المهمة مع تحديث التقدم (مع حد انتظار لمنع التعليق)"""
        last_status = None
        last_progress = -1
        started = time.time()
        while True:
            if max_wait_sec and (time.time() - started) > max_wait_sec:
                return {
                    "status": "failed",
                    "error_code": "timeout",
                    "message": "Client wait timeout",
                }
            result = await self.get_job_status(job_id)

            if "error" in result:
                return result

            status = result.get("status")
            progress = int(result.get("progress", 0) or 0)
            message = result.get("message", "Processing...")
            detail = result.get("progress_detail") or {}
            step = detail.get("step")
            if step and step not in message:
                message = f"{message} · {step}"
            error_code = result.get("error_code")
            if status == "failed" and error_code:
                message = f"{message} (code={error_code})"

            if progress_callback and (
                status != last_status or progress != last_progress
            ):
                await progress_callback(progress, 100, message)
                last_status = status
                last_progress = progress

            if status == "completed":
                return result
            elif status == "failed":
                return result

            await asyncio.sleep(2)  # فحص كل ثانيتين

    async def sync_custom_data(
        self, custom_sites: dict, site_auth: dict, custom_selectors: dict | None = None
    ):
        """يرسل المواقع المخصصة + auth + custom selectors للـ Worker لمزامنتها"""
        data = {
            "custom_sites": custom_sites,
            "site_auth": site_auth,
            "custom_selectors": custom_selectors or {},
        }
        return await self._post("/sync_custom_data", data)

    async def radar_check(
        self, url: str, last_chapter: float, max_new: int = 10
    ) -> dict:
        """
        يطلب من HF Worker فحص السلسلة وإرجاع آخر فصل + الفصول الجديدة مع lock info.
        """
        data = {
            "url": url,
            "last_chapter": float(last_chapter or 0),
            "max_new": int(max_new),
        }
        return await self._post("/radar/check", data)

    async def radar_cover(self, url: str) -> str | None:
        """
        يطلب غلاف السلسلة من HF Worker.
        يرجع رابط الغلاف أو None.
        """
        res = await self._post("/radar/cover", {"url": url})
        if res.get("ok") and res.get("cover_url"):
            return res["cover_url"]
        return None

    async def get_chapters_with_lock_info(self, url: str) -> dict:
        """
        يطلب فصول المانجا مع معلومات الأقفال من HF Worker.
        """
        res = await self._post("/radar/check", {"url": url, "last_chapter": 0, "max_new": 999999})
        if isinstance(res, dict) and res.get("ok"):
            out = {}
            for ch in res.get("new_chapters", []):
                out[ch["num"]] = {
                    "url": ch["url"],
                    "locked": ch["locked"],
                    "reason": ch["reason"],
                }
                if ch.get("unlock_time"):
                    out[ch["num"]]["unlock_time"] = ch["unlock_time"]
            return out
        return {}

    async def get_all_chapters(self, url: str) -> dict:
        """يطلب استخراج الفصول من HF Worker."""
        res = await self._post("/extract/chapters", {"url": url})
        if isinstance(res, dict) and res.get("ok"):
            return {float(k): v for k, v in res.get("chapters", {}).items()}
        return {}
