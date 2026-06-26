@echo off
chcp 65001 >nul
title نظام الحجز
echo ============================================
echo   نظام الحجز الكامل
echo ============================================
echo.
echo [1] استيراد المنشآت من بيانات السحب (output)
echo [2] تشغيل خادم الحجز (http://localhost:5001)
echo [3] عرض المنشآت + روابطها + PIN
echo.
set /p choice="اختر (1/2/3): "

if "%choice%"=="1" (
  python booking_system.py import
  echo.
  pause
) else if "%choice%"=="3" (
  python booking_system.py list
  echo.
  pause
) else (
  python booking_system.py run
)
