@echo off
setlocal
title AutoClaw Batch Login

echo.
echo  ===================================================
echo     AutoClaw Auto-Login - Batch Mode
echo  ===================================================
echo.

REM Check proxy.py running
curl -s http://localhost:31000/health >nul 2>&1
if errorlevel 1 goto :noproxyserver
echo  [OK] Proxy server running on http://localhost:31000
echo.
goto :checkaccounts

:noproxyserver
echo  [!] Proxy server is NOT running!
echo      Start it first: double-click start-proxy.bat
echo.
pause
exit /b 1

:checkaccounts
if exist "%~dp0accounts.txt" goto :run
echo  [!] accounts.txt not found!
echo      Create it with email:password per line
echo.
pause
exit /b 1

:run
python "%~dp0autoclaw_autologin.py" --batch "%~dp0accounts.txt" --interactive %*

echo.
echo  ===================================================
echo  Script finished.
echo  Dashboard: http://localhost:31000
echo  ===================================================
echo.
cmd /k
