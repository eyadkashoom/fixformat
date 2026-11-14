# Project Status

مصدر الحقيقة الوحيد لحالة fixformat. يُحدَّث عند كل حدث معتمد (دمج فرع،
وَسْم إصدار، فتح PR). الأرقام هنا تأتي من `git log` و`VERSION` في القرص،
ليس من نصوص المحادثة.

آخر تحديث: 2026-11-14 — PR #2 مفتوح، الفرع `add1f22` (0 behind / 1 ahead).

---

## 1. الإصدار الحالي

| البند | القيمة | المرجع |
|---|---|---|
| `VERSION` في `raw_scan.py` | `7.0.3` | قرص |
| أحدث commit على `main` | `350bd5b` (merge commit) | `git log -1 origin/main` |
| آخر وَسْم منشور | `v7.0.3` | GitHub Releases |

---

## 2. الإصدارات المنشورة

| الإصدار | الوسم | commit | الحالة |
|---|---|---|---|
| v7.0.1 | `v7.0.1` | `d508835` | منشور |
| v7.0.1.2 | `v7.0.1.2` | `8a73938` | منشور |
| v7.0.2 | — | — | مخطّط (لم يُوسَم بعد) |
| v7.0.3 | `v7.0.3` | `e113241` | منشور |
| v7.0.3 + golden fixtures | — | `350bd5b` | مدموج في `main`، لم يُوسَم |

---

## 3. المهام المنجزة (بأدلة على القرص)

### ✅ إصلاح أداء `text_search.py` + العيّنات الذهبية
- **الفرع:** `fix/text-recall-golden-fixtures`
- **الدمج في `main`:** `350bd5b` (PR #1)
- **CI:** أخضر (build 1m7s)
- **الأدلة:**
  - `fixtures/` — 14 ملفًا (135 KiB) + `MANIFEST.json` +
    `scripts/generate_fixtures.py` (seed 24301).
  - `test_fixtures.py` 13/13 ✅
  - `test_text_search.py` 30/30 ✅ (على `main` بعد PR #1؛ 34/34 على
    الفرع `feat/text-search-stream` بعد إضافة اختبارات المعاينة الأربع)
  - `test_text_search_perf.py` 8/8 ✅ (بوابة CI)
- **الأخطاء التي كُشفت وأُصلحت (مُثبتة من QA):**
  1. العربية UTF-16LE كانت غير مرئية في الشيفرة المحسّنة (`aa3af86`)،
     وليست بقعة عمياء في `main` — الانحدار وُلد داخل الفرع وأُصلح.
  2. UTF-16LE عند إزاحة فردية (odd parity) كان يُسقط — أُصلح بفحص
     parity ثانية.
  3. المعاينة فُكّت بترميز خاطئ عند parity خاطئة — أُصلحت بمحاذاة
     code-unit.
  4. منطقة واحدة كانت تجمع ترميزين — `_absorb()` ينهي المنطقة عند تغيّر
     الترميز.
- **الأداء المقاس (10 MiB لكل corpus، تشغيل QA المستقل على `08f335c`):**
  - zeros/noise/compressed: 15–27 MiB/s (هدف القبول ≥ 10 ✅)
  - ASCII 0.64 MiB/s / Arabic UTF-8 0.57 MiB/s / UTF-16LE 1.02 MiB/s
    (سقف Python خالص، موثق في `perf/README.md`)
  - Peak RSS < 90 MiB، scaling خطي (noise 10.2× لـ 10× المدخل)

### ✅ Golden Fixtures Set
- **الموقع:** `fixtures/` + `fixtures/README.md` + `fixtures/MANIFEST.json`
- **التحقق في CI:** `generate_fixtures.py --check` + `test_fixtures.py`
  يُشغَّلان في `python-app.yml`.
- **النتيجة:** 4 سلبيات (صفر مناطق)، 6 إيجابيات (تغطية ≥ 0.9)، 4 حالات
  حدية (BOM، truncation، misaligned UTF-16LE، sparse text).
- **ملاحظة:** `positive/utf16be_english.txt` حقل `acceptance.why` في
  `MANIFEST.json` يشرح أن تسمية `utf-16le` دقيقة بايتيًا — لا مسار
  `utf-16be` منفصل في الطبقات النصية، ويُلتقط عبر parity فردة لـ
  UTF-16LE فقط.

---

## 4. المهام المفتوحة

### 🟡 جارية — بانتظار إعادة التأسيس
**`feat/text-search-stream` — Layer 5 streaming**
- **المسؤول:** @core_developer (إعادة تأسيس) ثم @lead (PR #2).
- **الحالة على القرص:** الفرع عند `add1f22`، `0 behind / 1 ahead` من
  `origin/main` — جاهز للدمج المباشر.
- **المُنجَز على الفرع:**
  - `search_stream(fileobj, chunk=8 MiB)` — يقرأ كتلًا ثابتة مع تداخُل
    `window + step`، لا يتطلب `seek` ولا `tell`.
  - `search_file()` صار يُنادي `search_stream(fh)` بدل `fh.read()` —
    الذاكرة O(chunk) لا O(حجم الملف).
  - إصلاحان إضافيان: NUL guard (`"\\x00"` → `"\x00"`)، وإسقاط U+FFFD من
    بداية المعاينة.
  - `test_text_search_stream.py` — 10 تأكيدات ✅، CI أخضر ✅، تكافؤ
    بايتي مع `search()` على العيّنات الـ 14 ✅.
- **QA:** @qa_automated_tester وافق `add1f22` (65/65 اختبارًا، صفر
  `ResourceWarning`).
- **التوثيق:** مسوّدة `[Unreleased]` جاهزة عند @technical_writer_docs
  بانتظار إعادة التأسيس، ستُودَع مع PR #2.

### 🔴 غير مُنفَّذ — Backlog موحد (معتمد من @lead، بانتظار التنفيذ)

| # | البند | يعتمد على | المسؤول المقترح |
|---|---|---|---|
| 0 | إصلاح `TextRecoveryGuide.md`: إزالة التكرارات، توحيد الترقيم مع Layer 1–5، وسم [Implemented]/[Planned]/[Deferred] | — | @technical_writer_docs |
| 5 | توحيد المحرك: إلغاء الكاشف الخاص في `raw_scan` وتوجيه CLI-ين عبر `text_recovery_pipeline` | — | @core_developer |
| 4 | streaming (البند 4) | — | ✅ مُنجز على الفرع، بانتظار الدمج |
| 1 | N-Gram Perplexity full (قيمة + ranking، ليس threshold gate) | 5 | @core_developer |
| 2 | ترتيب النتائج + تفكيك confidence (chain/gate/ngram/score) | 1 | @core_developer |
| 6a | السقف (1000 ملف / 64 MiB) — blocker | — | @core_developer |
| 6b | JSON report output | 2 | @core_developer |
| 3 | دعم الترميزات الناقصة (UTF-16BE، CP1256، BOM) | 1 | @core_developer |

**فجوات إضافية من QA (مُضافة للـ Backlog):**
- `ngram_filter` لا يُصدّر confidence مفكّكًا — التقرير يأخذ رقمًا
  مركّبًا لا يُشرَّح.
- `text_search.search()` تتطلب bytes كاملة — ✅ حُلّ على الفرع
  (`search_stream`).
- أقسام مكررة في `TextRecoveryGuide.md` — ✅ ستُزال في البند 0.
- Performance test لم يكن جزءًا من CI — ✅ أُضيف في `python-app.yml`.

**شروط قبول إلزامية لكل بند:**
- هدف رقمي قابل للقياس (FP=0 / recall ≥ 99% / MiB/s).
- عيّنة اختبار ثابتة في المستودع (`fixtures/`).
- نتيجة متوقّعة مُحدَّدة مسبقًا.
- عدم تراجع (regression test).
- Definition of Done: لا يُغلق بند تقني بدون تحديث
  `TextRecoveryGuide.md` + `CHANGELOG` + (عند اللزوم) User Guide.

---

## 5. المشكلات المعروفة

| المشكلة | الأثر | السبب | الحالة |
|---|---|---|---|
| الصورة الحقيقية 476.9 GiB غير مختبرة | أكبر ادعاء جاهزية بلا دليل | `OSError 22` على `F:/hard512/...img.partial` — القرص الخارجي غير موصول | محجوبة على العتاد، ليست خلل كود |
| `text_ngram` بوابة ثنائية لا ranking | لا قيمة `exp(-lp)` ولا درجة مُصدَّرة | خارج نطاق الدفعة الحالية | موثَّق في CHANGELOG: بوابة accept/reject لا ranking |
| لا مسار `utf-16be` | `utf16be_english.txt` يُلتقط عبر parity فردة لـ UTF-16LE | خارج نطاق الطبقات النصية الحالية | حقل `acceptance.why` في `MANIFEST.json` |
| `test_text_classify.py` — SQLite connection leak | `ResourceWarning` عند التشغيل | leak سابق لوصوله | خارج نطاق دفعة البث، سيُعالَج مستقلاً |
| نموذج trigram V=87 / 2498 trigram فقط | نص خارج أبجديته يهبط للأرضية | نموذج موحّد لكل فئات اللغة | بند 3 في الـ Backlog |

---

## 6. دورة الاعتماد (مُثبتة)

```
Developer → QA → Fixes (if needed) → QA Approval → Lead Review → User Approval
```

- **PR #1:** `fix/text-recall-golden-fixtures` → `main` — ✅ مُدمَج (`350bd5b`)
- **PR #2:** `feat/text-search-stream` → `main` — 🟡 مفتوح، بانتظار الدمج بعد المراجعة

---

## 7. المسؤوليات (كما أقرّها المستخدم)

| الدور | الشخص | الاختصاص |
|---|---|---|
| قرار نهائي | @user | نطاق المشروع، الأولويات، انتقال المراحل |
| تنفيذي | @lead | إدارة الفريق، توزيع المهام، متابعة التسليمات، فتح PRs |
| تنفيذ | @core_developer | كتابة الكود + أدلة قابلة لإعادة التحقق |
| تحقق مستقل | @qa_automated_tester | قبول/رفض بأدلة، صلاحية رفض أي تسليم |
| توثيق | @technical_writer_docs | توثيق المنتج الفعلي على القرص، لا الخطط |
