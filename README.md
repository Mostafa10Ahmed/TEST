<div align="center">

# ⚡ MANGA AUTOMATION & AI INPAINTING SYSTEM v3.0 ⚡
### *Enterprise-Grade Manga Downloader, Management Dashboard & AI Text Erase Engine*

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Discord.py](https://img.shields.io/badge/Discord.py-v2.3%2B-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://github.com/Rapptz/discord.py)
[![Slash Commands](https://img.shields.io/badge/Discord-Slash%20Commands-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.com/developers/docs/interactions/application-commands)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100%2B-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PyTorch](https://img.shields.io/badge/PyTorch-CUDA%20Accelerated-EE4C2C?style=for-the-badge&logo=pytorch&logoColor=white)](https://pytorch.org/)
[![HuggingFace](https://img.shields.io/badge/HuggingFace-ZeroGPU%20Enabled-FFD21E?style=for-the-badge&logo=huggingface&logoColor=black)](https://huggingface.co/)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

<br/>

> **نظام متكامل عالي الكفاءة لإدارة، تنزيل، وتبييض فصول المانجا والمانهوا بالذكاء الاصطناعي، يربط بين بوت ديسكورد يعتمد بالكامل على أوامر السلاش (`/` Slash Commands) ولوحة التفاعل المتقدمة `/manga_panel`، لوحة تحكم ويب إدارية، وخوادم معالجة GPU.**

---

[📖 نبذة عن النظام](#-نبذة-عن-النظام) •
[⚡ أوامر السلاش](#-أوامر-السلاش-slash-commands) •
[⚙️ الأقسام الرئيسية](#️-الأقسام-الرئيسية) •
[🏛️ البنية الهندسية](#️-البنية-الهندسية) •
[🌐 المواقع المدعومة](#-المواقع-المدعومة) •
[🚀 التشغيل السريع](#-التشغيل-السريع) •
[📚 التوثيق التفصيلي](#-التوثيق-التفصيلي)

---

</div>

<br/>

## 📖 نبذة عن النظام (System Overview)

منظومة برمجية متطورة مصممة لإدارة وتنزيل وتبييض فصول المانجا والمانهوا الكورية واليابانية والصينية. تعتمد على معمارية الخدمات الدقيقة (**Microservices Architecture**) مع الدعم الكامل والتلقائي لأوامر السلاش (`/`) في ديسكورد عبر `discord.app_commands`.

يتكون المشروع من **قسمين رئيسيين**:

```text
                               ┌────────────────────────────────────────┐
                               │       ⚡ Manga System Platform         │
                               └───────────────────┬────────────────────┘
                                                   │
                         ┌─────────────────────────┴─────────────────────────┐
                         ▼                                                   ▼
       ┌───────────────────────────────────┐               ┌───────────────────────────────────┐
       │         🤖 bot-core               │               │       🎨 inpainting-core          │
       │ ├─ Native Discord Slash Commands  │               │ ├─ AI LaMa Inpainting Engine      │
       │ ├─ Interactive /manga_panel View │               │ ├─ Hard Mask Text Erase System    │
       │ ├─ FastAPI Web Admin Dashboard    │               │ ├─ FastAPI / Gradio AI Server     │
       │ ├─ 30+ Provider Scrapers Engine   │               │ └─ ZeroGPU & RTX 6000 Optimized   │
       │ ├─ HuggingFace Worker Offloader   │               │                                   │
       │ └─ Google Drive Cloud Sync        │               └───────────────────────────────────┘
       └───────────────────────────────────┘
```

---

## ⚡ أوامر السلاش (Discord Slash Commands)

يعتمد البوت بشكل كامل وأساسي على نظام **Slash Commands (`/`)** لتوفير تجربة تفاعلية سلسة وقوائم منسدلة وخيارات إكمال أوتوماتيكي (Autocomplete):

| أمر السلاش | الوظيفة | الصلاحيات |
| :--- | :--- | :--- |
| `/manga_panel` | **فتح لوحة التفاعل المتقدمة الشاملة لاختيار وتنزيل الفصول (Interactive UI)** | VIP / المشتركين |
| `/download` | تنزيل وتنسيق فصول المانجا/المانهوا مباشرة | للجميع / المشتركين |
| `/clean_manga` | تنزيل الفصل وتبييضه بالذكاء الاصطناعي عبر الـ GPU | للمشتركين / المشرفين |
| `/search` | البحث المباشر عن العناوين في المواقع المدعومة | للجميع |
| `/mytracker` | عرض قائمة المتابعات والرادار الشخصي الخاص بك | للجميع |
| `/tracker` | إدارة رادار التتبع التلقائي للفصول والاشتراكات | المشرفين |
| `/stitch_drive` | دمج وقص الصور المرفوعة على Google Drive | VIP / المشتركين |
| `/extract` | استخراج وتنسيق نصوص الفصول والترجمة | المشتركين |
| `/help` | قائمة المساعدة وتفاصيل استخدام البوت والخيارات | للجميع |
| `/panel` / `!panel` | رابط ورسالة الوصول الفوري للوحة تحكم الويب | المشرفين |

> **ملاحظة**: يقوم البوت عند التشغيل تلقائياً بربط وأجتياز تزامن الأوامر (`await bot.tree.sync()`) لظهور جميع أوامر السلاش مباشرة في سيرفرات الديسكورد.

---

## ⚙️ الأقسام الرئيسية (Core Modules)

### 1️⃣ قسم الإدارة والتحميل (`bot-core`)
المحرك الرئيسي والمركز التنفيذي للنظام:
- **🤖 بوت الديسكورد (Slash Commands Native)**: معالجة الأوامر التفاعلية، الخيارات المباشرة، وقوائم الواجهة الجرافيكية (Interactive UI Components, Select Menus, Buttons).
- **💻 لوحة تحكم الويب (Web Admin Panel)**: واجهة مستخدم مبنية بـ FastAPI لعرض الإحصائيات المباشرة، إدارة المشتركين، ضبط المواقع، ومتابعة سجلات النظام الحية.
- **🔍 محرك Scrapers شامل**: يدعم أكثر من 30+ موقع مانجا مع تجاوز حمايات Cloudflare وتحديات الـ JavaScript.
- **☁️ مزامنة Google Drive**: رفع الفصول تلقائياً وإنشاء مجلدات وروابط مشاركة مباشرة.
- **⚡ HuggingFace Worker (`hf_worker`)**: خدمة مدمجة يمكن نشرها كعامل بعيد لتخفيف أحمال التنزيل الثقيلة عن خادم البوت الرئيسي.

### 2️⃣ قسم التبييض بالذكاء الاصطناعي (`inpainting-core`)
خادم مخصص يعمل على بطاقات الـ GPU لإزالة النصوص والفقاعات من المانجا:
- **🧠 نموذج LaMa Deep Inpainting**: إزالة فقاعات الكلام والنصوص الخارجية بدقة فائقة وبدون تشويه الخلفيات (Zero Ghosting Text).
- **🎯 الماسك التلقائي الذكي**: كشف أماكن النصوص وتوليد القناع المخصص (Mask Generation) بدقة دون المساس بالرسومات الأصلية.
- **🚀 توافق مع ZeroGPU**: ديكوريتور `@spaces.GPU` يتوافق تماماً مع HuggingFace ZeroGPU لضمان أعلى أداء واستجابة مجانية وسريعة.

---

## 🏛️ البنية الهندسية وتدفق البيانات (Architecture & Data Flow)

يوضح المخطط التسلسلي كيفية سريان البيانات عند استخدام أمر السلاش `/clean_manga` أو لوحة `/manga_panel`:

```mermaid
sequenceDiagram
    autonumber
    actor User as 👤 المستخدم / المشرف
    participant Bot as 🤖 bot-core (Slash Command /)
    participant Scraper as 🔍 Providers Scraper
    participant Inpaint as 🎨 inpainting-core (GPU)
    participant Drive as ☁️ Google Drive

    User->>Bot: إرسال أمر السلاش (/manga_panel أو /clean_manga)
    Bot->>Scraper: استخراج روابط وجداول صور الفصل
    Scraper-->>Bot: إرجاع روابط الصور المباشرة
    
    rect rgb(30, 41, 59)
        note over Bot,Inpaint: معالجة الذكاء الاصطناعي (Parallel AI Processing)
        Bot->>Inpaint: إرسال دفعة الصور عبر HTTP POST
        Inpaint->>Inpaint: كشف الفقاعات + تطبيق نموذج LaMa GPU
        Inpaint-->>Bot: إرجاع الصور النظيفة الخالية من النصوص
    end

    Bot->>Bot: تطبيق الدمج الذكي (smart_stitch)
    Bot->>Drive: رفع الصور وتوليد مجلد ورابط الفصل
    Drive-->>Bot: إرجاع رابط Google Drive
    Bot-->>User: إرسال النتيجة ورابط الفصل المبيض بالديسكورد
```

---

## 🌐 المواقع المدعومة (Supported Providers)

يدعم النظام محركات استخراج مخصصة لأشهر منصات المانجا والمانهوا:

| اسم الموقع | نوع المحرك | دعم الحماية |
| :--- | :--- | :--- |
| **Asura Scans** | API / Custom Scraper | ✅ Cloudflare Bypass |
| **Comix** | JavaScript Nonce / Scrapling | ✅ Hydration Bypass |
| **MangaDex** | Official REST API | ✅ Direct Native |
| **Kakao Webtoon** | GraphQL / BFF API | ✅ Authenticated Data |
| **Naver Webtoon** | Custom HTML Parser | ✅ High Speed |
| **LekManga / Madara** | WordPress Ajax Scraper | ✅ Auto-Pagination |
| **QiManhwa / Utoon** | Custom DOM Evaluators | ✅ Playwright Backup |

---

## 🚀 التشغيل السريع (Quick Start Guide)

### 1. استنساخ المشروع (Clone Repository)

```bash
git clone https://github.com/more249-s/dis-inpainting.git
cd dis-inpainting
```

---

### 2. تشغيل قسم `bot-core`

```bash
cd bot-core

# 1. إنشاء وتفعيل البيئة الافتراضية
python -m venv venv
# Linux/macOS: source venv/bin/activate
# Windows: venv\Scripts\activate

# 2. تثبيت المكتبات
pip install -r requirements.txt

# 3. إعداد المتغيرات البيئية
cp .env.example .env

# 4. تشغيل البوت ولوحة الويب
python main.py
```

---

### 3. تشغيل قسم `inpainting-core`

```bash
cd inpainting-core

# 1. إنشاء البيئة الافتراضية وتثبيت التبعات
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. تشغيل الخادم
python app.py
```

---

## 📱 لوحة التحكم والأوامر (Dashboard & Commands)

### 💻 لوحة تحكم الويب (Web Admin Panel)
عند تشغيل `bot-core` يمكنك الوصول إلى لوحة الويب عبر: `http://localhost:8000`

- 📈 **إحصائيات مباشرة**: استهلاك الذاكرة، المعالج، والتحميلات النشطة.
- 👥 **إدارة الأعضاء**: تحديد الرتب والاشتراكات.
- 🔍 **متابعة السجلات**: مراقبة أداء الخادم والمواقع حياً.

---

## 📚 التوثيق التفصيلي (Documentation Index)

للحصول على شرح تفصيلي وعميق لكل كود ومكون بالمشروع، يمكنك مراجعة المستندات التالية:

- 📖 **[ARCHITECTURE.md](ARCHITECTURE.md)**: توثيق البنية الهندسية وتدفق الكود وتفاعل النماذج.
- 🚀 **[DEPLOYMENT.md](DEPLOYMENT.md)**: خطوات النشر التفصيلية على VPS و Docker و HuggingFace ZeroGPU.
- 🌐 **[PROVIDERS.md](PROVIDERS.md)**: دليل تطوير وتحديث محركات جلب المواقع والسكيربرز.
- 🤖 **[bot-core/README.md](bot-core/README.md)**: دليل تشغيل وإدارة البوت ولوحة الويب وعامل التنزيل.
- 🎨 **[inpainting-core/README.md](inpainting-core/README.md)**: توثيق API التبييض ونماذج الذكاء الاصطناعي.
