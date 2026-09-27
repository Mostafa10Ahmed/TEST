FROM python:3.10-slim

# تثبيت متطلبات النظام لمعالجة الصور و Playwright
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# نسخ الملفات الأساسية
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# تحديد مسار مخصص لمتصفحات Playwright لضمان إمكانية القراءة والكتابة والتشغيل لأي مستخدم
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# تثبيت متصفح Chromium مع كافة اعتمادياته كـ root في المجلد المشترك
RUN playwright install chromium
RUN playwright install-deps
RUN chmod -R 777 /ms-playwright

# نسخ الكود البرمجي
COPY . .

# إنشاء مجلدات العمل وإعطاء صلاحيات كاملة 777 لتجنب مشاكل الصلاحيات على HuggingFace
RUN mkdir -p /app/data /app/temp_downloads && chmod -R 777 /app

# تهيئة مستخدم غير جذر (UID 1000) متوافق مع معايير HuggingFace Spaces
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# HuggingFace يحتاج تشغيل على بورت 7860 عادةً
ENV PORT=7860
EXPOSE 7860

CMD ["uvicorn", "hf_worker.app:app", "--host", "0.0.0.0", "--port", "7860"]
