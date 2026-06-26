@echo off
chcp 65001 >nul
title أداة سحب Google Maps - واجهة الويب
echo ============================================
echo   تشغيل واجهة الويب لأداة سحب Google Maps
echo ============================================
echo.

:: تحقق من Flask
python -c "import flask" 2>nul
if errorlevel 1 (
    echo [!] Flask غير مثبت. جارٍ التثبيت...
    python -m pip install flask
)

:: تحقق من Playwright
python -c "import playwright" 2>nul
if errorlevel 1 (
    echo [!] Playwright غير مثبت. شغّل install.bat أولاً.
    pause
    exit /b 1
)

echo.
echo  بدء الخادم... سيُفتح المتصفح تلقائياً
echo  العنوان: http://localhost:5000
echo.

:: افتح المتصفح بعد ثانيتين (في الخلفية)
start "" cmd /c "timeout /t 3 >nul && start http://localhost:5000"

:: شغّل الخادم
python app.py

pause
