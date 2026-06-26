@echo off
chcp 65001 >nul
echo ============================================
echo   أداة سحب بيانات Google Maps - أمثلة
echo ============================================
echo.
echo اختر الوضع:
echo   [1] بحث بكلمة ومدينة
echo   [2] استخراج موقع واحد من رابط
echo   [3] استخراج قائمة من ملف input_list.txt
echo   [0] خروج
echo.
set /p choice="اختيارك: "

if "%choice%"=="1" (
    set /p keyword="كلمة البحث (مثال: مطاعم): "
    set /p city="المدينة (مثال: القاهرة): "
    set /p maxn="عدد النتائج [20]: "
    if "%maxn%"=="" set maxn=20
    python run.py search --keyword "%keyword%" --city "%city%" --max %maxn% --no-headless
)
if "%choice%"=="2" (
    set /p url="الصق رابط الموقع: "
    python run.py url --url "%url%" --no-headless
)
if "%choice%"=="3" (
    python run.py file --input input_list.txt --no-headless
)
if "%choice%"=="0" exit /b 0
echo.
pause
