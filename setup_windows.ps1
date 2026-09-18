# إعداد APEX Ultimate V2 على ويندوز — شغّله من داخل مجلد المشروع:
#   powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1

Write-Host "`n=== APEX Ultimate V2 — إعداد ===" -ForegroundColor Cyan

# 1) فحص بايثون
try { $v = (python --version) 2>&1; Write-Host "Python: $v" -ForegroundColor Green }
catch { Write-Host "بايثون غير مثبت. نزّله من python.org ثم أعد التشغيل." -ForegroundColor Red; exit 1 }

# 2) مفاتيح Binance من متغيرات البيئة
if (-not $env:BINANCE_API_KEY) {
  $k = Read-Host "ألصق BINANCE_API_KEY الجديد (اتركه فارغاً للتخطي)"
  if ($k) { setx BINANCE_API_KEY $k | Out-Null; $env:BINANCE_API_KEY = $k }
}
if (-not $env:BINANCE_API_SECRET) {
  $s = Read-Host "ألصق BINANCE_API_SECRET الجديد (اتركه فارغاً للتخطي)"
  if ($s) { setx BINANCE_API_SECRET $s | Out-Null; $env:BINANCE_API_SECRET = $s }
}

# 3) Telegram اختياري لإشارات Top 100
if (-not $env:TELEGRAM_TOKEN) {
  $t = Read-Host "توكن Telegram (اختياري — Enter للتخطي)"
  if ($t) { setx TELEGRAM_TOKEN $t | Out-Null; $env:TELEGRAM_TOKEN = $t
            $c = Read-Host "TELEGRAM_CHAT_ID"; if ($c) { setx TELEGRAM_CHAT_ID $c | Out-Null; $env:TELEGRAM_CHAT_ID = $c } }
}

# 4) الاختبارات
Write-Host "`n--- اختبار محرك Top 100 ---" -ForegroundColor Cyan
python -m apex_top100.tests.test_engine
Write-Host "`n--- اختبار الدمج داخل APEX_ULTIMATE ---" -ForegroundColor Cyan
python -m apex_top100.tests.test_apex_hook

Write-Host "`nجاهز." -ForegroundColor Green
Write-Host "  python run_top100.py --demo     محاكاة Top 100 بدون إنترنت"
Write-Host "  python APEX_ULTIMATE.py         البوت كاملاً + Top 100 على http://localhost:8080"
Write-Host "ملاحظة: setx لا يؤثر على النافذة الحالية — افتح PowerShell جديدة إن ضبطت المفاتيح الآن." -ForegroundColor Yellow
