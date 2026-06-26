@echo off
chcp 65001 >nul
title خط الإنتاج الكامل
echo ============================================
echo   خط الإنتاج: استخراج ^< فرز ^< حِزَم عرض
echo ============================================
echo.
set /p kw="كلمة البحث (مثال: صالونات تجميل): "
set /p ct="المدينة (مثال: الرياض): "
set /p mx="عدد النتائج (مثال: 10): "
echo.
python pipeline.py --keyword "%kw%" --city "%ct%" --max %mx% --import-booking
echo.
echo افتح لوحة الحملة: output\outreach\index.html
pause
