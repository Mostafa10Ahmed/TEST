# 🚀 دليل النشر والتشغيل الشامل (Deployment Guide)

دليل مفصل لنشر وتشغيل **Manga Downloader & AI Inpainting System** على خوادم VPS وحاويات Docker و HuggingFace Spaces.

---

## 📋 متطلبات التشغيل

| المكون | البيئة الموصى بها | المتطلبات |
| :--- | :--- | :--- |
| **`bot-core`** | خادم VPS (Ubuntu 22.04+) أو Docker | Python 3.10+, 2GB RAM |
| **`inpainting-core`** | HuggingFace ZeroGPU Space أو VPS مزود بـ GPU | PyTorch, CUDA 11.8+, VRAM 8GB+ |

---

## 🤖 1. نشر قسم `bot-core`

### التشغيل على خادم Linux VPS

```bash
# 1. تحديث النظام وتثبيت التبعات
sudo apt update && sudo apt upgrade -y
sudo apt install python3 python3-pip python3-venv git -y

# 2. استنساخ المشروع ودخول المجلد
git clone https://github.com/your-username/manga-system.git
cd manga-system/bot-core

# 3. إنشاء البيئة الافتراضية وتثبيت المكتبات
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. إعداد المتغيرات البيئية
cp .env.example .env
nano .env
```

#### التشغيل كخدمة خلفية (Systemd Service)

أنشئ ملف الخدمة:
```bash
sudo nano /etc/systemd/system/manga-bot.service
```

ضع المحتوى التالي:
```ini
[Unit]
Description=Manga Bot Core Service
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/manga-system/bot-core
ExecStart=/home/ubuntu/manga-system/bot-core/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

قم بتشغيل وتفعيل الخدمة:
```bash
sudo systemctl daemon-reload
sudo systemctl enable manga-bot
sudo systemctl start manga-bot
```

---

## 🎨 2. نشر قسم `inpainting-core` على HuggingFace Spaces (ZeroGPU)

1. افتح حسابك في HuggingFace وأنشر Space جديد:
   - **SDK**: **Gradio**
   - **Hardware**: **ZeroGPU**
2. قم برفع محتويات مجلد `inpainting-core/`:
   - `app.py`
   - `requirements.txt`
   - `.gitattributes`
3. سيعمل الخادم تلقائياً ويعطيك رابط الـ Space مثل:
   `https://username-inpainting-core.hf.space`
4. ضع هذا الرابط في ملف `.env` الخاص بـ `bot-core` تحت متغير `INPAINTING_API_URL`.
