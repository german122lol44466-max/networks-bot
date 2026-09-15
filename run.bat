@echo off
cd /d "%~dp0"
if not exist .env (
  echo [!] No .env file. Copy .env.example to .env and paste your token.
  pause & exit /b
)
rem pip can't use SOCKS proxies from VPN apps - clear them for the install step only
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set http_proxy=
set https_proxy=
set all_proxy=
python -m pip install -r requirements.txt --proxy "" -q
if errorlevel 1 (
  echo.
  echo [!] Failed to install libraries. Check internet / disable VPN and run again.
  pause & exit /b
)
:loop
python bot.py
echo Bot stopped, restarting in 5 sec... (close this window to quit)
timeout /t 5 >nul
goto loop
