"""
dl_queue.py — Auto-download queue for tracker v3.

Handles:
  - Queued chapter downloads with per-tracker concurrency limits
  - Drive folder auto-creation per series
  - DB-backed status (pending/completed/failed) — crash recovery safe
  - Anti-duplicate: checks DB before starting any download
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.notifier_v3 import NotifierV3
    from remote_downloader import RemoteDownloader

import database

logger = logging.getLogger("sv3.dl_queue")

MAX_CONCURRENT_DOWNLOADS = 3   # كم تحميل يعمل بالتوازي
MAX_QUEUE_SIZE = 200


def _sanitize_folder_name(name: str) -> str:
    """Solo Leveling (manga) → Solo Leveling"""
    name = re.sub(r"[<>:\"/\\|?*]", "", name)
    name = re.sub(r"\s{2,}", " ", name).strip()
    return name[:100] or "Series"


class DLQueue:
    """
    طابور تحميل ذكي — يدعم:
    - التزامن المحدود (max 3 في وقت واحد)
    - فولدر Drive مخصص لكل سلسلة (يُنشأ تلقائياً عند أول تحميل)
    - Crash recovery: يكمل الـ pending عند إعادة التشغيل
    - Anti-duplicate: يفحص DB قبل أي تحميل
    """

    def __init__(self, remote_down: "RemoteDownloader", notifier: "NotifierV3", bot):
        self.remote_down = remote_down
        self.notifier = notifier
        self.bot = bot
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._sem = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        self._task: asyncio.Task | None = None
        # مفتاح لكل (tracker_id, chapter_num) لمنع التزامن على نفس الفصل
        self._active_keys: set[tuple] = set()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._worker(), name="sv3-dl-worker")
        logger.info("DLQueue worker started")

    async def close(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    # ── Public API ────────────────────────────────────────────────────────────

    async def enqueue(
        self,
        tracker: dict,
        event: dict,
    ) -> bool:
        """
        يضيف فصل لطابور التحميل.
        يرجع False لو في تحميل يعمل بالفعل لنفس الفصل.
        """
        key = (tracker["id"], event["chapter_num"])
        if key in self._active_keys:
            logger.debug(f"[DLQueue] Already queued: tracker#{tracker['id']} ch{event['chapter_num']}")
            return False

        # فحص DB (طبقة 2)
        existing = await database.tevt_get(tracker["id"], event["chapter_num"])
        if existing and existing["dl_status"] in ("completed", "pending"):
            logger.debug(f"[DLQueue] Already in DB ({existing['dl_status']}), skipping")
            return False

        self._active_keys.add(key)
        try:
            await self._queue.put((tracker, event))
            return True
        except asyncio.QueueFull:
            self._active_keys.discard(key)
            logger.warning(f"[DLQueue] Queue full, dropping {key}")
            return False

    async def recover_pending(self) -> int:
        """
        يُشغَّل عند بدء البوت — يُعيد الـ pending من الـ crash.
        يرجع عدد الأحداث التي أُعيد تشغيلها.
        """
        pending = await database.tevt_get_pending_downloads()
        count = 0
        for p in pending:
            tracker = await database.sv3_get(p["tracker_id"], p["guild_id"])
            if not tracker:
                continue
            event = await database.tevt_get(p["tracker_id"], p["chapter_num"])
            if not event:
                continue
            queued = await self.enqueue(tracker, event)
            if queued:
                count += 1
        logger.info(f"[DLQueue] Crash recovery: re-queued {count} pending downloads")
        return count

    # ── Worker ────────────────────────────────────────────────────────────────

    async def _worker(self) -> None:
        while True:
            try:
                tracker, event = await self._queue.get()
                asyncio.create_task(self._process(tracker, event))
                self._queue.task_done()
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[DLQueue] Worker error: {e}", exc_info=True)
                await asyncio.sleep(2)

    async def _process(self, tracker: dict, event: dict) -> None:
        key = (tracker["id"], event["chapter_num"])
        async with self._sem:  # max concurrent downloads
            try:
                await self._do_download(tracker, event)
            finally:
                self._active_keys.discard(key)

    async def _do_download(self, tracker: dict, event: dict) -> None:
        tid = tracker["id"]
        guild_id = tracker["guild_id"]
        chapter_num = event["chapter_num"]
        chapter_url = event.get("chapter_url") or ""
        series_title = tracker.get("title") or ""

        logger.info(f"[DLQueue] ⬇️  Starting: {series_title} Ch.{chapter_num}")

        # Ensure we have the latest event data (which contains alert_message_id)
        db_evt = await database.tevt_get(tid, chapter_num)
        if db_evt:
            event = db_evt

        # Update status to Stitching
        await self._update_alert_status(event, tracker, "⚙️ Stitching...")

        # ── Mark pending in DB ─────────────────────────────────────────────
        await database.tevt_set_dl_pending(event["id"])

        # ── Ensure Drive folder exists ─────────────────────────────────────
        folder_url = await self._ensure_drive_folder(tracker)

        # Spawn task to update to Uploading... in 8 seconds
        async def delayed_update():
            try:
                await asyncio.sleep(8)
                await self._update_alert_status(event, tracker, "📤 Uploading...")
            except asyncio.CancelledError:
                pass
        up_task = asyncio.create_task(delayed_update())

        # ── Submit to remote worker ────────────────────────────────────────
        try:
            result = await self.remote_down.start_download(
                url=chapter_url,
                title=f"{_sanitize_folder_name(series_title)}_ch{chapter_num}",
                job_type="manga",
                params={
                    "target_folder": tracker.get("drive_folder_id") or "",
                    "series_name": series_title,
                    "chapter_num": str(chapter_num),
                },
            )
        except Exception as e:
            logger.error(f"[DLQueue] remote_down error: {e}")
            await database.tevt_set_dl_failed(event["id"], str(e))
            await self.notifier.enqueue_download_complete(
                tracker, event,
                drive_url=None, failed=True, failed_reason=str(e)[:200],
            )
            return
        finally:
            up_task.cancel()

        # ── Parse result ───────────────────────────────────────────────────
        if isinstance(result, dict):
            ok       = result.get("success") or result.get("status") == "completed"
            dl_url   = result.get("download_url") or result.get("result_url") or ""
            drive_id = result.get("drive_file_id") or ""
        else:
            ok = bool(result)
            dl_url = str(result) if result else ""
            drive_id = ""

        if ok and dl_url:
            await database.tevt_set_dl_completed(event["id"], dl_url, drive_id)
            # Increment folder chapter counter
            await database.tdf_increment(tid)
            # Update tracker with latest chapter
            await database.sv3_update(tid, guild_id, last_chapter=chapter_num)
            logger.info(f"[DLQueue] ✅ Done: {series_title} Ch.{chapter_num} → {dl_url[:60]}")
            await self.notifier.enqueue_download_complete(
                tracker, event, drive_url=dl_url or folder_url,
            )
        else:
            reason = result.get("error", "Unknown error") if isinstance(result, dict) else "Download failed"
            await database.tevt_set_dl_failed(event["id"], reason[:200])
            logger.error(f"[DLQueue] ❌ Failed: {series_title} Ch.{chapter_num} — {reason}")
            await self.notifier.enqueue_download_complete(
                tracker, event,
                drive_url=None, failed=True, failed_reason=reason[:200],
            )

    async def _update_alert_status(self, event: dict, tracker: dict, label: str):
        alert_msg_id = event.get("alert_message_id")
        channel_id = tracker.get("notification_channel_id")
        if not alert_msg_id or not channel_id:
            return
        try:
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                channel = await self.bot.fetch_channel(int(channel_id))
            msg = await channel.fetch_message(int(alert_msg_id))
            
            view = discord.ui.LayoutView.from_message(msg)
            updated = False
            for item in view.children:
                if isinstance(item, discord.ui.Button) and item.custom_id and item.custom_id.startswith("sv3_dl_"):
                    item.label = label
                    item.disabled = True
                    item.style = discord.ButtonStyle.secondary
                    updated = True
            if updated:
                await msg.edit(view=view)
        except Exception as e:
            logger.debug(f"[DLQueue] Could not update button status to {label}: {e}")

    # ── Drive folder creation ─────────────────────────────────────────────────

    async def _ensure_drive_folder(self, tracker: dict) -> str | None:
        """يضمن وجود فولدر Drive للسلسلة ويرجع رابطه."""
        tid = tracker["id"]
        existing = await database.tdf_get(tid)
        if existing and existing.get("folder_url"):
            return existing["folder_url"]

        # أنشئ فولدر جديد
        series_name = _sanitize_folder_name(tracker.get("title") or "Series")
        try:
            result = await self.remote_down.create_remote_folder(
                folder_name=series_name,
                upload_dest="drive",
            )
            if isinstance(result, dict):
                folder_id  = result.get("folder_id", "")
                folder_url = result.get("folder_url", "")
            else:
                folder_id, folder_url = "", ""

            await database.tdf_upsert(tid, series_name, folder_id, folder_url)
            # حفظ folder_id في الـ tracker أيضاً
            await database.sv3_update(tid, tracker["guild_id"],
                                      drive_folder_id=folder_id,
                                      drive_folder_url=folder_url)
            logger.info(f"[DLQueue] 📁 Created Drive folder: {series_name} → {folder_url or folder_id}")
            return folder_url or None
        except Exception as e:
            logger.warning(f"[DLQueue] Failed to create Drive folder: {e}")
            return None
