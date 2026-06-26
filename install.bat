@echo off
chcp 65001 >nul
echo ============================================
echo   تثبيت متطلبات أداة سحب Google Maps
echo ============================================
echo.
echo [1/2] تثبيت مكتبات بايثون...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ❌ فشل تثبيت المتطلبات.
    pause
    exit /b 1
)
echo.
echo [2/2] تثبيت متصفح Playwright (Chromium)...
python -m playwright install chromium
if errorlevel 1 (
    echo.
    echo ❌ فشل تثبيت المتصفح.
    pause
    exit /b 1
)
echo.
echo ============================================
echo   ✓ تم التثبيت بنجاح!
echo   شغّل: run.bat  لرؤية الأمثلة
echo ============================================
pause
