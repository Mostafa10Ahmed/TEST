import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

logger = logging.getLogger("mangasystem.adaptive_poller")

class AdaptivePoller:
    """
    نظام الـ Polling التكيفي الذكي (Adaptive Polling System)
    يتحكم في فترات فحص التراكرز بناءً على:
    1. درجة حرارة العمل (Heat Score) - ترتفع عند صدور الفصول وتنخفض بالتدريج.
    2. الأولوية المحددة يدوياً (Manual Priority).
    3. طريقة الفحص (API, RSS, Scrape) لتجنب الحظر.
    4. تعلم نمط جدول الإصدارات (Schedule Learning).
    """

    # فترات الفحص لكل فئة (بالثواني)
    INTERVALS = {
        "instant": 30,      # 30 ثانية (للمانجا المهمة جداً)
        "fast": 120,       # دقيقتين (بعد الصدور مباشرة)
        "normal": 300,     # 5 دقائق (الافتراضي)
        "slow": 900,       # 15 دقيقة
        "idle": 3600       # ساعة واحدة (الأعمال المتوقفة أو النادرة)
    }

    def __init__(self):
        pass

    def get_effective_interval(self, priority: str, heat_score: float, check_method: str) -> int:
        """
        حساب الفترة الفعالة بالفحص بالثواني بناءً على الأولوية، الحرارة، وطريقة الفحص.
        """
        # 1. تحديد الفئة بناءً على الأولوية اليدوية أو الحرارة
        priority = (priority or "normal").lower()
        if priority == "auto":
            # تحديد الفئة بناءً على الـ Heat Score
            if heat_score >= 80:
                tier = "instant"
            elif heat_score >= 60:
                tier = "fast"
            elif heat_score >= 30:
                tier = "normal"
            elif heat_score >= 10:
                tier = "slow"
            else:
                tier = "idle"
        else:
            tier = priority

        interval = self.INTERVALS.get(tier, 300)

        # 2. قيود ذكية بناءً على طريقة الفحص (تجنب الحظر)
        check_method = (check_method or "scrape").lower()
        if tier == "instant":
            if check_method in ("api", "mangadex_api", "comick_api"):
                interval = 15
            elif check_method == "rss":
                interval = 30
            else: # scrape
                interval = 60
        else:
            if check_method == "scrape":
                # الكشط المباشر لا يقل عن دقيقة (60 ثانية) بأي حال من الأحوال
                interval = max(60, interval)
            elif check_method == "rss":
                # الـ RSS لا يقل عن 30 ثانية
                interval = max(30, interval)
            elif check_method in ("api", "mangadex_api", "comick_api"):
                # الـ APIs الرسمية مسموح فحصها حتى 15 ثانية لو كانت أولويتها عالية
                interval = max(15, interval)

        return interval


    def update_heat(self, current_heat: float, event: str) -> float:
        """
        تحديث درجة الحرارة بناءً على الأحداث (Event-driven heat update).
        current_heat: الحرارة الحالية (0-100)
        event: نوع الحدث ('new_chapter', 'no_change', 'error', 'schedule_boost')
        """
        new_heat = current_heat
        if event == "new_chapter":
            new_heat += 40.0
        elif event == "schedule_boost":
            new_heat = max(new_heat, 85.0)  # رفع الحرارة فوراً للاستعداد للإصدار
        elif event == "error":
            new_heat -= 10.0  # خفض الحرارة عند الأخطاء لتقليل الضغط (Backoff)
        elif event == "no_change":
            # انخفاض تلقائي طفيف
            new_heat -= 0.5

        return max(0.0, min(100.0, new_heat))

    def decay_heat_by_time(self, current_heat: float, last_checked_str: str, now: datetime) -> float:
        """
        حساب انخفاض الحرارة التلقائي بناءً على الوقت المنقضي (2 درجة لكل ساعة).
        """
        if not last_checked_str:
            return current_heat
        try:
            last_checked = datetime.fromisoformat(last_checked_str)
            # التأكد من مطابقة المنطقة الزمنية أو جعلها naive
            if last_checked.tzinfo is not None:
                last_checked = last_checked.replace(tzinfo=None)
            if now.tzinfo is not None:
                now = now.replace(tzinfo=None)
                
            elapsed_hours = (now - last_checked).total_seconds() / 3600.0
            if elapsed_hours > 0:
                decay = elapsed_hours * 2.0  # معدل الانخفاض: 2 درجات لكل ساعة
                return max(0.0, current_heat - decay)
        except Exception as e:
            logger.error(f"Error in decay_heat_by_time: {e}")
        return current_heat

    def learn_schedule(self, current_pattern_json: Optional[str], release_time: datetime) -> str:
        """
        تعلم وتحديث نمط مواعيد إصدار الفصول.
        release_time: وقت صدور الفصل الجديد.
        يرجع السلسلة النصية لنمط الإصدار المحدث بتنسيق JSON.
        """
        # النمط الافتراضي
        pattern = {"samples": [], "confidence": 0.0, "day": -1, "hour": -1}
        if current_pattern_json:
            try:
                pattern = json.loads(current_pattern_json)
            except Exception:
                pass

        # إضافة عينة جديدة (يوم الأسبوع 0-6، الساعة 0-23)
        day = release_time.weekday()
        hour = release_time.hour
        
        samples = pattern.get("samples", [])
        samples.append({"day": day, "hour": hour, "timestamp": release_time.isoformat()})
        
        # الاحتفاظ بآخر 10 عينات فقط
        if len(samples) > 10:
            samples = samples[-10:]
            
        pattern["samples"] = samples

        # حساب الثقة وتحديد اليوم والساعة الأكثر تكراراً لو توفرت عينات كافية
        if len(samples) >= 3:
            # حساب التكرارات
            day_counts = {}
            hour_counts = {}
            for s in samples:
                d = s["day"]
                h = s["hour"]
                day_counts[d] = day_counts.get(d, 0) + 1
                hour_counts[h] = hour_counts.get(h, 0) + 1
                
            best_day = max(day_counts, key=day_counts.get)
            best_hour = max(hour_counts, key=hour_counts.get)
            
            # الثقة = تكرار اليوم الأفضل / إجمالي العينات
            confidence = day_counts[best_day] / len(samples)
            
            pattern["day"] = best_day
            pattern["hour"] = best_hour
            pattern["confidence"] = round(confidence, 2)
        else:
            pattern["confidence"] = 0.0

        return json.dumps(pattern)

    def is_schedule_release_near(self, pattern_json: Optional[str], now: datetime) -> bool:
        """
        التحقق مما إذا كان موعد الصدور المتوقع قريباً (خلال ساعة من الآن) وثقة التوقع عالية (> 0.7).
        """
        if not pattern_json:
            return False
        try:
            pattern = json.loads(pattern_json)
            day = pattern.get("day", -1)
            hour = pattern.get("hour", -1)
            confidence = pattern.get("confidence", 0.0)
            
            if day == -1 or hour == -1 or confidence < 0.7:
                return False
                
            # التحقق هل اليوم هو نفس اليوم المتوقع والساعة الحالية تقترب من الساعة المتوقعة
            # مثلاً: إذا كان الإصدار متوقعاً في الساعة 17 والآن بين 16 و 17
            current_day = now.weekday()
            current_hour = now.hour
            
            if current_day == day:
                # التحقق لو كنا قبل الموعد المتوقع بساعة واحدة
                diff_hours = hour - current_hour
                if diff_hours == 1 or (diff_hours == 0 and now.minute <= 30):
                    return True
        except Exception as e:
            logger.error(f"Error in is_schedule_release_near: {e}")
        return False

    async def get_due_trackers(self, all_trackers: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
        """
        تصفية وتحديد التراكرز المستحقة للفحص بناءً على فتراتها الفعالة.
        all_trackers: قائمة التراكرز مع كافة بياناتها وحالتها الحالية.
        """
        due_trackers = []
        for tr in all_trackers:
            paused = tr.get("paused", 0)
            if paused:
                continue

            last_checked_str = tr.get("last_checked")
            priority = tr.get("priority", "normal")
            heat_score = tr.get("heat_score", 50.0)
            check_method = tr.get("check_method", "scrape")
            release_pattern = tr.get("release_pattern")

            # 1. تقليل الحرارة زمنياً
            heat_score = self.decay_heat_by_time(heat_score, last_checked_str, now)
            tr["heat_score"] = heat_score  # حفظ القيمة المقدرة

            # 2. تعزيز الحرارة لو موعد الصدور المتوقع قريباً
            if self.is_schedule_release_near(release_pattern, now):
                # تعزيز الحرارة وتحديث الفئة للـ instant مؤقتاً
                heat_score = self.update_heat(heat_score, "schedule_boost")
                tr["heat_score"] = heat_score
                tr["_schedule_boosted"] = True

            # 3. حساب الفترة المطلوبة بالفحص بالثواني
            interval_seconds = self.get_effective_interval(priority, heat_score, check_method)
            tr["_effective_interval"] = interval_seconds

            # 4. مقارنة الوقت المنقضي
            if not last_checked_str:
                due_trackers.append(tr)
                continue

            try:
                last_checked = datetime.fromisoformat(last_checked_str)
                if last_checked.tzinfo is not None:
                    last_checked = last_checked.replace(tzinfo=None)
                if now.tzinfo is not None:
                    now = now.replace(tzinfo=None)
                    
                elapsed = (now - last_checked).total_seconds()
                if elapsed >= interval_seconds:
                    due_trackers.append(tr)
            except Exception as e:
                logger.error(f"Error checking tracker due status: {e}")
                due_trackers.append(tr)

        return due_trackers
