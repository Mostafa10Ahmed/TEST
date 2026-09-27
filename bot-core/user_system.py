"""
نظام صلاحيات المستخدمين — 3 رتب:
  3 = Owner  (من ALLOWED_USER_IDS — مدمج في الكود)
  2 = VIP    (تحميل مانجا + SmartStitch)
  1 = User   (بحث + قراءة)
  0 = مرفوض  (لا يقدر يستعمل البوت)
"""

import functools
import discord
from discord import app_commands
from bot_config import Config
import database


RANK_LABELS = {
    4: "👑 Owner",
    3: "🛡️ Admin",
    2: "⭐ VIP",
    1: "👤 User",
    0: "🚫 Blocked",
}

RANK_COLORS = {
    4: discord.Color.from_rgb(255, 184, 0),
    3: discord.Color.from_rgb(255, 100, 0), # لون برتقالي للأدمن
    2: discord.Color.from_rgb(99, 102, 241),
    1: discord.Color.from_rgb(56, 189, 248),
    0: discord.Color.from_rgb(239, 68, 68),
}


# ── جلب رتبة المستخدم ──────────────────────────────────────────────────────
async def get_rank(user_id: int, auto_register: bool = True) -> int:
    """إرجاع رتبة المستخدم (0-4)."""
    if user_id in Config.ALLOWED_USER_IDS:
        return 4
    return await database.get_user_rank(user_id, auto_register=auto_register)


def is_owner(user_id: int) -> bool:
    return user_id in Config.ALLOWED_USER_IDS


# ── مساعد لإرسال رسائل ephemeral بأمان ─────────────────────────────────
async def _safe_reply(interaction: discord.Interaction, content: str):
    """إرسال رسالة إلى interaction بدون crash حتى لو انتهت صلاحيتها."""
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content, ephemeral=True)
        else:
            await interaction.followup.send(content, ephemeral=True)
    except discord.NotFound:
        pass  # Interaction expired — لا يهم
    except Exception:
        pass


# ── فحص الصلاحية ───────────────────────────────────────────────────────────
async def check_rank(interaction: discord.Interaction, min_rank: int) -> bool:
    rank = await get_rank(interaction.user.id)
    if rank >= min_rank:
        return True

    if rank == 0:
        msg = "❌ ليس لديك صلاحية استخدام هذا البوت.\nتواصل مع المالك للحصول على وصول."
    else:
        msg = f"❌ هذا الأمر يحتاج رتبة **{RANK_LABELS.get(min_rank, str(min_rank))}** أو أعلى."

    await _safe_reply(interaction, msg)
    return False


# ── مزخرف (Decorator) لفحص الرتبة ─────────────────────────────────────────
def require_rank(min_rank: int):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            interaction = None
            for a in args:
                if isinstance(a, discord.Interaction):
                    interaction = a
                    break
            if interaction is None:
                return
            if not await check_rank(interaction, min_rank):
                return
            return await func(*args, **kwargs)
        return wrapper
    return decorator


# ── ديكوراتورات app_commands.check ──────────────────────────────────────────
def owner_only():
    """يُرجع app_commands.check للأوامر التي تستخدم @bot.tree.command (تشمل الأدمن والمالك)"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if await interaction.client.is_owner(interaction.user):
            return True

        rank = await get_rank(interaction.user.id)
        ok = rank >= 3
        if not ok:
            await _safe_reply(interaction, "❌ هذا الأمر مخصص للإدارة (أدمن/مالك) فقط.")
        return ok
    return app_commands.check(predicate)


def vip_only():
    """يُرجع app_commands.check لأوامر VIP+"""
    async def predicate(interaction: discord.Interaction) -> bool:
        rank = await get_rank(interaction.user.id)
        ok   = rank >= 2
        if not ok:
            await _safe_reply(
                interaction,
                "❌ هذا الأمر يحتاج رتبة ⭐ VIP أو أعلى.\nتواصل مع المالك للترقية.",
            )
        return ok
    return app_commands.check(predicate)


def user_only():
    """يُرجع app_commands.check لأوامر User+ (أي مستخدم مسجّل)"""
    async def predicate(interaction: discord.Interaction) -> bool:
        rank = await get_rank(interaction.user.id)
        ok   = rank >= 1
        if not ok:
            await _safe_reply(
                interaction,
                "❌ ليس لديك صلاحية استخدام هذا البوت.\nتواصل مع المالك للحصول على وصول.",
            )
        return ok
    return app_commands.check(predicate)


import datetime
import time

# Rate limit configuration: Max 5 requests per 30 seconds
RATE_LIMIT_WINDOW = 30  # seconds
RATE_LIMIT_MAX = 5
REQUEST_HISTORY: dict[int, list[float]] = {}


async def check_and_consume_usage(user_id: int, action_type: str, pages_count: int = 1) -> tuple[bool, str]:
    """
    Checks if a user can perform 'clean' or 'extract' action based on rank, trial, and credit limits.
    If pages_count > 35, cost is 2 chapter units; otherwise 1 unit.
    If yes, consumes credits/daily usage/trial and returns (True, message).
    If no, returns (False, warning_message).
    """
    rank = await get_rank(user_id)
    if rank >= 3:
        # Admin or Owner (no limits)
        return True, ""

    if rank == 0:
        return False, "❌ حسابك محظور من استخدام البوت."

    # Spam Protection Check
    now_ts = time.time()
    history = REQUEST_HISTORY.get(user_id, [])
    # Filter out timestamps older than the window
    history = [t for t in history if now_ts - t < RATE_LIMIT_WINDOW]
    history.append(now_ts)
    REQUEST_HISTORY[user_id] = history

    if len(history) > RATE_LIMIT_MAX:
        # Spam detected! Demote rank to 0 (Blocked)
        await database.set_user_rank(user_id, 0, "Blocked for spamming requests")
        return False, "❌ تم حظرك تلقائياً من استخدام البوت بسبب إرسال طلبات متعددة بسرعة فائقة (Spamming)."

    cost = 2 if pages_count > 35 else 1
    cost_msg = f" (تم خصم {cost} وحدة كونه فصل ضخم > 35 صفحة)" if cost > 1 else ""

    today_str = datetime.date.today().isoformat()
    credits_data = await database.get_user_credits(user_id)

    if rank == 2:
        # VIP limits: 5 clean_manga / 5 extract per day.
        # Otherwise, check credits.
        usage = await database.get_user_daily_usage(user_id, today_str)
        if action_type == "clean":
            if usage["clean_count"] + cost <= 5:
                for _ in range(cost):
                    await database.increment_user_daily_usage(user_id, today_str, "clean")
                return True, f"⭐ تم استخدام {cost} من حدك اليومي المجاني للـ VIP{cost_msg}."
            elif credits_data["inpainting_credits"] >= cost:
                await database.add_user_credits(user_id, inpainting=-cost)
                return True, f"⭐ تم استخدام {cost} من نقاط رصيدك الإضافية للتبييض{cost_msg}."
            else:
                return False, f"❌ رصيدك أو حدك اليومي غير كافٍ لمعالجة هذا الفصل (يتطلب {cost} وحدات).\nيمكنك طلب نقاط إضافية من الإدارة."
        else: # extract
            if usage["extract_count"] + cost <= 5:
                for _ in range(cost):
                    await database.increment_user_daily_usage(user_id, today_str, "extract")
                return True, f"⭐ تم استخدام {cost} من حدك اليومي المجاني للـ VIP{cost_msg}."
            elif credits_data["extraction_credits"] >= cost:
                await database.add_user_credits(user_id, extraction=-cost)
                return True, f"⭐ تم استخدام {cost} من نقاط رصيدك الإضافية للاستخراج{cost_msg}."
            else:
                return False, f"❌ رصيدك أو حدك اليومي غير كافٍ لمعالجة هذا الفصل (يتطلب {cost} وحدات).\nيمكنك طلب نقاط إضافية من الإدارة."

    # Rank 1: Normal User (Exactly 1 trial lifetime, otherwise check credits)
    if action_type == "clean":
        if not credits_data["used_trial_clean"]:
            await database.set_user_trial_used(user_id, "clean")
            return True, "🎁 لقد استخدمت تجربتك المجانية المرة الواحدة المسموحة للتبييض."
        elif credits_data["inpainting_credits"] >= cost:
            await database.add_user_credits(user_id, inpainting=-cost)
            return True, f"⭐ تم استهلاك {cost} من نقاط رصيدك للتبييض{cost_msg}."
        else:
            return False, "❌ لقد انتهت تجربتك المجانية للتبييض (فصل واحد مدى الحياة).\nيرجى التواصل مع الإدارة للترقية للـ VIP أو شحن رصيد نقاط."
    else: # extract
        if not credits_data["used_trial_extract"]:
            await database.set_user_trial_used(user_id, "extract")
            return True, "🎁 لقد استخدمت تجربتك المجانية المرة الواحدة المسموحة للاستخراج."
        elif credits_data["extraction_credits"] >= cost:
            await database.add_user_credits(user_id, extraction=-cost)
            return True, f"⭐ تم استهلاك {cost} من نقاط رصيدك للاستخراج{cost_msg}."
        else:
            return False, "❌ لقد انتهت تجربتك المجانية للاستخراج والترجمة (فصل واحد مدى الحياة).\nيرجى التواصل مع الإدارة للترقية للـ VIP أو شحن رصيد نقاط."


# ── Cooldown & Active Task Locks ──────────────────────────────────────────────
COMMAND_COOLDOWN_SECONDS = 60  # 60 seconds (1 minute) cooldown between commands
USER_LAST_COMMAND_TIME: dict[int, float] = {}
USER_ACTIVE_LOCKS: set[int] = set()


def check_user_cooldown_and_lock(user_id: int, rank: int = 1) -> tuple[bool, str]:
    """
    Checks if a user is currently running a task or in 60s cooldown.
    Admins/Owners (rank >= 3) bypass cooldown, but active task lock applies to prevent race conditions.
    """
    if user_id in USER_ACTIVE_LOCKS:
        return False, "⚠️ لديك عملية تبييض أو معالجة قيد التنفيذ حالياً، يرجى الانتظار حتى الانتهاء منها أولاً!"

    if rank < 3:
        last_time = USER_LAST_COMMAND_TIME.get(user_id, 0.0)
        now = time.time()
        elapsed = now - last_time
        if elapsed < COMMAND_COOLDOWN_SECONDS:
            rem = int(COMMAND_COOLDOWN_SECONDS - elapsed)
            return False, f"⏳ يرجى الانتظار `{rem}` ثانية قبل استخدام هذا الأمر مجدداً (مهلة 60 ثانية بين كل أمر والآخر)."

    return True, ""


def acquire_user_lock(user_id: int):
    """Marks user as busy with an active task."""
    USER_ACTIVE_LOCKS.add(user_id)


def release_user_lock(user_id: int):
    """Releases active task lock and records completion timestamp for 60s cooldown."""
    USER_ACTIVE_LOCKS.discard(user_id)
    USER_LAST_COMMAND_TIME[user_id] = time.time()
