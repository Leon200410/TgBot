@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "data\qr-login" mkdir "data\qr-login"
echo 正在准备扫码登录工具，不会停止当前 Telegram 服务。
docker compose build --quiet tgbot
if errorlevel 1 goto failed
echo 二维码显示后，用手机 Telegram 的“设置 - 设备 - 扫描二维码”扫码。
echo 也可以打开本目录 data\qr-login\telegram_login.png 查看最新二维码。
docker compose run --rm --no-deps -v "%~dp0data\qr-login:/login" tgbot python qr_login.py --session /login/tg_session.session --qr-file /login/telegram_login.png
if errorlevel 1 goto failed
echo 登录凭证保存在：%~dp0data\qr-login\tg_session.session
pause
exit /b 0
:failed
echo 扫码登录未完成，请查看上面的提示。
pause
exit /b 1
