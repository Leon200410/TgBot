# -*- coding: utf-8 -*-
"""手机扫码授权并保存独立 Telegram 会话，不切换正在运行的账号。"""

import argparse
import asyncio
import getpass
import math
import os
import sys
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import qrcode
from telethon import TelegramClient
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError


class QRLoginError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoginResult:
    account_id: int
    username: str | None
    already_authorized: bool


def render_qr(url: str, path: Path):
    """输出可扫码 PNG 和终端二维码，不打印登录 URL/token 的明文。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    try:
        qr.make_image(fill_color="black", back_color="white").save(temporary, format="PNG")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    try:
        qr.print_ascii(invert=True)
    except UnicodeEncodeError:
        print("当前终端无法显示二维码，请打开保存的 PNG 图片扫码。")


def prompt_two_factor_password() -> str:
    if not sys.stdin.isatty():
        raise QRLoginError("账号开启了两步验证，请在交互终端中运行脚本后输入密码。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass("Telegram 两步验证密码（输入不显示）：")
    except (getpass.GetPassWarning, EOFError):
        raise QRLoginError("当前终端无法隐藏密码输入，请改用交互终端运行。") from None


def _remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise QRLoginError("等待扫码已超时，请重新运行脚本。")
    return seconds


async def _request(operation, deadline: float | None = None):
    timeout = 30 if deadline is None else min(30, _remaining(deadline))
    try:
        return await asyncio.wait_for(operation(), timeout=timeout)
    except asyncio.TimeoutError:
        raise QRLoginError("连接或登录请求超时，请检查网络后重试。") from None


async def _password_login(client, password_prompt, log):
    for attempt in range(3):
        password = password_prompt()
        if not password:
            raise QRLoginError("未输入两步验证密码，扫码登录未完成。")
        try:
            return await _request(lambda: client.sign_in(password=password))
        except PasswordHashInvalidError:
            log(f"两步验证密码不正确，还可重试 {2 - attempt} 次。")
        finally:
            password = None
    raise QRLoginError("两步验证密码连续错误，登录未完成，请重新运行。")


def _result(user, *, already_authorized: bool) -> LoginResult:
    if user is None or not getattr(user, "id", None):
        raise QRLoginError("未能确认登录账号，登录未完成。")
    return LoginResult(user.id, getattr(user, "username", None), already_authorized)


async def _cancel_wait(task):
    if task is not None:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _cleanup_qr(path: Path, log):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log(f"二维码图片未能自动清理，请手动删除：{path}")


async def run_qr_login(
    client,
    *,
    qr_file: Path,
    timeout_seconds: float = 300,
    render: Callable[[str, Path], None] = render_qr,
    password_prompt: Callable[[], str] = prompt_two_factor_password,
    log: Callable[[str], None] = print,
) -> LoginResult:
    """扫码期限覆盖全部扫码网络请求；人工输入两步密码单独处理。"""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise QRLoginError("扫码等待秒数必须大于零。")
    qr_file = Path(qr_file)
    deadline = time.monotonic() + timeout_seconds
    wait_task = None
    rendered = False
    try:
        await _request(client.connect, deadline)
        if await _request(client.is_user_authorized, deadline):
            user = await _request(client.get_me, deadline)
            log("这个会话已经登录，直接保留并复用，无需再次扫码。")
            return _result(user, already_authorized=True)

        qr = await _request(client.qr_login, deadline)
        log("手机 Telegram → 设置 → 设备 → 扫描二维码，然后确认登录。")
        while True:
            remaining = _remaining(deadline)
            lifetime = (qr.expires - datetime.now(timezone.utc)).total_seconds()
            if lifetime <= 0:
                await _request(qr.recreate, deadline)
                continue
            wait_task = asyncio.create_task(qr.wait(timeout=min(lifetime, remaining)))
            # wait() 在内部注册更新监听，先让它启动再展示，避免快速扫码丢事件。
            await asyncio.sleep(0)
            rendered = True
            render(qr.url, qr_file)
            log(f"二维码图片：{qr_file}；过期后会自动刷新。")
            try:
                # wait() 内部仅约束更新等待；外层还约束确认和跨数据中心迁移请求。
                user = await asyncio.wait_for(wait_task, timeout=_remaining(deadline))
                log("手机扫码授权成功。")
                return _result(user, already_authorized=False)
            except asyncio.TimeoutError:
                _remaining(deadline)
                # 扫码恰好与到期同时发生时，先核对授权，避免成功后还刷新二维码。
                if await _request(client.is_user_authorized, deadline):
                    user = await _request(client.get_me, deadline)
                    log("手机扫码授权成功。")
                    return _result(user, already_authorized=False)
                log("二维码已过期，正在刷新；请扫描最新的二维码。")
                await _request(qr.recreate, deadline)
            finally:
                await _cancel_wait(wait_task)
                wait_task = None
    except SessionPasswordNeededError:
        # 扫码接受与到期刷新竞争时，初次生成/刷新请求也可能直接要求两步验证。
        if rendered:
            _cleanup_qr(qr_file, log)
        log("扫码已确认，账号开启了两步验证，还需要输入密码。")
        user = await _password_login(client, password_prompt, log)
        log("扫码和两步验证成功。")
        return _result(user, already_authorized=False)
    finally:
        await _cancel_wait(wait_task)
        if rendered:
            _cleanup_qr(qr_file, log)
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10)
        except Exception:
            log("连接关闭未完成；请检查终端状态后重试。")


def read_settings(env_file: Path) -> dict[str, str]:
    """仅读取本脚本所需设置，环境变量优先，不修改 .env。"""
    keys = {"API_ID", "API_HASH", "DEVICE_MODEL", "SYSTEM_VERSION", "APP_VERSION"}
    values = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() in keys:
                values[key.strip()] = value.strip().strip("\"'")
    for key in keys:
        if key in os.environ:
            values[key] = os.environ[key].strip()
    return values


def default_session() -> Path:
    if Path("/data").is_dir() and sys.platform != "win32":
        return Path("/data/qr_login/tg_session.session")
    return Path(__file__).resolve().parent / "data" / "qr-login" / "tg_session.session"


def main() -> int:
    parser = argparse.ArgumentParser(description="手机扫码登录 Telegram，保存独立会话凭证")
    parser.add_argument("--session", type=Path, default=default_session(), help="会话文件位置，默认使用独立目录")
    parser.add_argument("--qr-file", type=Path, help="二维码 PNG 位置，默认与候选会话同目录")
    parser.add_argument("--timeout", type=float, default=300, help="扫码等待上限（秒，不含两步验证密码输入用时）")
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parent / ".env")
    args = parser.parse_args()
    session = args.session.expanduser().resolve()
    if not str(session).endswith(".session"):
        session = Path(str(session) + ".session")
    qr_file = args.qr_file.expanduser().resolve() if args.qr_file else session.parent / "telegram_login.png"
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("扫码等待秒数必须大于零。", file=sys.stderr)
        return 1
    if session == Path("/data/tg_session.session").resolve() and session.exists():
        print("这是当前主程序使用的会话，请使用默认的独立目录生成新凭证。", file=sys.stderr)
        return 1
    if qr_file == session:
        print("二维码图片和登录会话不能使用同一个文件路径。", file=sys.stderr)
        return 1
    if qr_file.suffix.lower() != ".png":
        print("二维码图片路径必须以 .png 结尾。", file=sys.stderr)
        return 1
    try:
        settings = read_settings(args.env_file)
        api_id = settings.get("API_ID", "")
        api_hash = settings.get("API_HASH", "")
        if not api_id.isdigit() or not api_hash:
            raise QRLoginError("缺少 API_ID/API_HASH，请先填写项目 .env。")
        session.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(
            str(session),
            int(api_id),
            api_hash,
            device_model=settings.get("DEVICE_MODEL") or "Z880 MB",
            system_version=settings.get("SYSTEM_VERSION") or "Windows 11",
            app_version=settings.get("APP_VERSION") or "1.0",
        )
        result = asyncio.run(run_qr_login(client, qr_file=qr_file, timeout_seconds=args.timeout))
        account = f"@{result.username}" if result.username else f"ID={result.account_id}"
        print(f"已登录：{account}")
        print(f"登录凭证已保存：{session}")
        print("当前运行的 Telegram 服务没有切换账号；凭证内容不会在终端输出。")
        return 0
    except QRLoginError as exc:
        print(f"扫码登录未完成：{exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("扫码登录已取消，现有登录凭证已保留。", file=sys.stderr)
        return 130
    except Exception as exc:
        # 登录响应的异常文本可能含 token，不输出完整异常或 traceback。
        print(f"扫码登录未完成（{type(exc).__name__}），请检查网络或配置后重试。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
