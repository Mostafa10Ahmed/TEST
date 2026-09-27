"""
notifier_v3.py — Sends Discord notifications for tracker v3.

Handles:
  - Sending the new-chapter alert container + role mention
  - Sending the download-complete container + personal mention
  - Crash-safe: logs to DB before sending so we can recover

Call flow:
  TrackerEngineV3 → NotifierV3.notify_new_chapter()
  DLQueue         → NotifierV3.notify_download_complete()
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

import database
from ui.tracker_containers_v3 import (
    build_new_chapter_alert,
    build_new_chapter_alert_buttons,
    build_download_complete,
    build_download_complete_buttons,
)

if TYPE_CHECKING:
    from discord.ext.commands import Bot

logger = logging.getLogger("sv3.notifier")


class NotifierV3:
    """
    مسؤول عن إرسال كل إشعارات tracker v3 إلى Discord.

    يستخدم asyncio.Queue لضمان إرسال الإشعارات بشكل متسلسل
    وعدم إغراق Discord بـ rate limits.
    """

    def __init__(self, bot: "Bot"):
        self.bot = bot
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._worker(), name="sv3-notifier-worker")
        logger.info("NotifierV3 worker started")

    async def close(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    # ── Public enqueue methods ────────────────────────────────────────────────

    async def enqueue_new_chapter(
        self,
        tracker: dict,
        event: dict,
    ) -> None:
        """
        يضيف إشعار فصل جديد لطابور الإرسال.

        tracker: سجل من server_trackers
        event:   سجل من tracker_events
        """
        await self._queue.put(("new_chapter", tracker, event))

    async def enqueue_download_complete(
        self,
        tracker: dict,
        event: dict,
        drive_url: str | None = None,
        failed: bool = False,
        failed_reason: str = "",
    ) -> None:
        """يضيف إشعار اكتمال تحميل لطابور الإرسال."""
        await self._queue.put((
            "dl_complete", tracker, event, drive_url, failed, failed_reason
        ))

    # ── Worker ────────────────────────────────────────────────────────────────

    async def _worker(self) -> None:
        while True:
            try:
                item = await self._queue.get()
                kind = item[0]
                if kind == "new_chapter":
                    _, tracker, event = item
                    await self._send_new_chapter(tracker, event)
                elif kind == "dl_complete":
                    _, tracker, event, drive_url, failed, failed_reason = item
                    await self._send_download_complete(
                        tracker, event, drive_url, failed, failed_reason
                    )
                self._queue.task_done()
                # صغير جداً لكن يمنع rate-limit burst
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Notifier] Worker error: {e}", exc_info=True)
                await asyncio.sleep(2)

    # ── Actual senders ────────────────────────────────────────────────────────

    async def _send_new_chapter(self, tracker: dict, event: dict) -> None:
        """
        يرسل كونتينر الإشعار مع منشن الرول.

        Content (text outside container): @RoleName
        Container: cover + chapter info + buttons
        """
        channel = await self._get_channel(tracker["notification_channel_id"])
        if not channel:
            logger.warning(
                f"[Notifier] Channel {tracker['notification_channel_id']} not found "
                f"for tracker #{tracker['id']}"
            )
            return

        series_title = tracker.get("title") or ""
        series_url   = tracker["url"]
        chapter_num  = event["chapter_num"]
        chapter_url  = event.get("chapter_url") or ""
        cover_url    = tracker.get("cover_url")
        detected_at  = event.get("detected_at")

        # Check if locked (provider_mgr)
        locked, has_cookies = await self._check_lock(series_url)

        # Build container
        container = build_new_chapter_alert(
            series_title=series_title,
            series_url=series_url,
            chapter_num=chapter_num,
            chapter_url=chapter_url,
            locked=locked,
            has_cookies=has_cookies,
            cover_url=cover_url,
            detected_at=detected_at,
        )
        buttons_row = build_new_chapter_alert_buttons(
            tracker_id=tracker["id"],
            chapter_num=chapter_num,
            chapter_url=chapter_url,
            locked=locked,
            has_cookies=has_cookies,
        )

        # Build LayoutView
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        view.add_item(buttons_row)

        # Mention content (outside container = plain text → role ping works)
        mention_content = ""
        if tracker.get("mention_role_id") and int(tracker.get("ping_on_update", 1)):
            mention_content = f"<@&{tracker['mention_role_id']}>"

        try:
            msg = await channel.send(content=mention_content or None, view=view)
            await database.tevt_mark_notified(event["id"], alert_message_id=str(msg.id))
            logger.info(
                f"[Notifier] ✅ Sent new chapter alert: "
                f"{series_title} Ch.{chapter_num} → #{channel.name}"
            )
        except discord.Forbidden:
            logger.error(f"[Notifier] No permission to send in {channel.id}")
        except discord.HTTPException as e:
            logger.error(f"[Notifier] HTTP error sending alert: {e}")

    async def _send_download_complete(
        self,
        tracker: dict,
        event: dict,
        drive_url: str | None,
        failed: bool,
        failed_reason: str,
    ) -> None:
        """
        يرسل كونتينر اكتمال التحميل مع منشن شخصي للمستخدم الذي أضاف التتبع.

        Content (text outside container): @user_id
        """
        channel = await self._get_channel(tracker["notification_channel_id"])
        if not channel:
            return

        series_title = tracker.get("title") or ""
        series_url   = tracker["url"]
        chapter_num  = event["chapter_num"]
        chapter_url  = event.get("chapter_url") or ""
        cover_url    = tracker.get("cover_url")

        container = build_download_complete(
            series_title=series_title,
            series_url=series_url,
            chapter_num=chapter_num,
            chapter_url=chapter_url,
            drive_url=drive_url,
            cover_url=cover_url,
            failed=failed,
            failed_reason=failed_reason,
        )
        buttons_row = build_download_complete_buttons(
            tracker_id=tracker["id"],
            chapter_num=chapter_num,
            chapter_url=chapter_url,
            failed=failed,
        )

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        view.add_item(buttons_row)

        # منشن شخصي للمستخدم الذي أضاف التتبع
        personal_mention = ""
        if tracker.get("added_by_user_id"):
            personal_mention = f"<@{tracker['added_by_user_id']}>"

        # Try to edit original alert message if available
        db_evt = await database.tevt_get(tracker["id"], chapter_num)
        alert_msg_id = db_evt.get("alert_message_id") if db_evt else None
        
        if alert_msg_id:
            try:
                msg = await channel.fetch_message(int(alert_msg_id))
                await msg.edit(content=personal_mention or None, view=view)
                logger.info(
                    f"[Notifier] ✅ Edited original alert message for completed download: "
                    f"{series_title} Ch.{chapter_num} "
                    f"({'FAILED' if failed else 'OK'})"
                )
                return
            except Exception as e:
                logger.warning(f"[Notifier] Could not edit original alert message {alert_msg_id}: {e}")

        try:
            await channel.send(content=personal_mention or None, view=view)
            logger.info(
                f"[Notifier] ✅ Sent download complete: "
                f"{series_title} Ch.{chapter_num} "
                f"({'FAILED' if failed else 'OK'})"
            )
        except discord.Forbidden:
            logger.error(f"[Notifier] No permission to send in {channel.id}")
        except discord.HTTPException as e:
            logger.error(f"[Notifier] HTTP error sending complete: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _get_channel(self, channel_id_str: str) -> discord.TextChannel | None:
        try:
            cid = int(channel_id_str)
            ch = self.bot.get_channel(cid)
            if ch is None:
                ch = await self.bot.fetch_channel(cid)
            return ch
        except Exception:
            return None

    async def _check_lock(self, url: str) -> tuple[bool, bool]:
        """يفحص إذا الفصل مقفل وإذا في كوكيز متاحة."""
        pm = getattr(self.bot, "provider_mgr", None)
        if pm is None:
            return False, False
        try:
            locked = await asyncio.wait_for(pm.is_locked(url), timeout=5)
            has_cookies = pm.has_auth_cookies(url)
            return bool(locked), bool(has_cookies)
        except Exception:
            return False, False
