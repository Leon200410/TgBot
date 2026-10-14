"""二维码登录的离线验证；不连接 Telegram，也不使用真实会话或二维码。"""

import asyncio
import tempfile
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
    from telethon.tl.types import User
    import qr_login
except ModuleNotFoundError as exc:
    if exc.name != "telethon":
        raise
    qr_login = None


QR_SECRET = "fixture://QR_URL_SECRET_MARKER"
PASSWORD_SECRET = "PASSWORD_SECRET_MARKER"
SESSION_SECRET = "SESSION_SECRET_MARKER"


class FakeQR:
    def __init__(self, events, user):
        self.events = events
        self.user = user
        self.url = QR_SECRET
        self.expires = datetime.now(timezone.utc) + timedelta(seconds=60)
        self.wait_effects = [user]
        self.recreate_effect = None
        self.wait_started = asyncio.Event()
        self.wait_cancelled = False
        self.wait_timeouts = []
        self.recreate_calls = 0

    async def wait(self, timeout=None):
        self.events.append("wait_started")
        self.wait_started.set()
        self.wait_timeouts.append(timeout)
        effect = self.wait_effects.pop(0) if self.wait_effects else self.user
        try:
            if isinstance(effect, BaseException):
                raise effect
            if callable(effect):
                return await effect()
            return effect
        except asyncio.CancelledError:
            self.wait_cancelled = True
            raise

    async def recreate(self):
        self.events.append("recreate")
        self.recreate_calls += 1
        self.wait_started.clear()
        if isinstance(self.recreate_effect, BaseException):
            raise self.recreate_effect
        if callable(self.recreate_effect):
            await self.recreate_effect()
        self.expires = datetime.now(timezone.utc) + timedelta(seconds=60)


class FakeClient:
    def __init__(self):
        self.events = []
        self.user = User(id=123, username="fixture_user")
        self.qr = FakeQR(self.events, self.user)
        self.authorized_effects = [False]
        self.stage_effects = {}
        self.sign_in_effects = [self.user]
        self.passwords = []
        self.connect_calls = 0
        self.get_me_calls = 0
        self.qr_login_calls = 0
        self.disconnect_calls = 0
        self.log_out_calls = 0
        self.session = SimpleNamespace(filename=SESSION_SECRET)

    async def _stage(self, name):
        self.events.append(name)
        effect = self.stage_effects.get(name)
        if isinstance(effect, BaseException):
            raise effect
        if callable(effect):
            await effect()

    async def connect(self):
        self.connect_calls += 1
        await self._stage("connect")

    async def is_user_authorized(self):
        await self._stage("is_user_authorized")
        if len(self.authorized_effects) > 1:
            return self.authorized_effects.pop(0)
        return self.authorized_effects[0]

    async def get_me(self):
        self.get_me_calls += 1
        await self._stage("get_me")
        return self.user

    async def qr_login(self):
        self.qr_login_calls += 1
        await self._stage("qr_login")
        return self.qr

    async def sign_in(self, *, password):
        self.passwords.append(password)
        await self._stage("sign_in")
        effect = self.sign_in_effects.pop(0) if self.sign_in_effects else self.user
        if isinstance(effect, BaseException):
            raise effect
        return effect

    async def disconnect(self):
        self.disconnect_calls += 1
        await self._stage("disconnect")

    async def log_out(self):
        self.log_out_calls += 1
        raise AssertionError("二维码登录不能注销已有会话")


@unittest.skipIf(qr_login is None, "二维码登录验证在包含 Telethon 的项目容器中运行")
class QRLoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.qr_file = self.root / "telegram-login.png"
        self.client = FakeClient()
        self.logs = []
        self.rendered_urls = []
        self.password_prompt_calls = 0

    def render(self, url, path):
        self.assertTrue(self.client.qr.wait_started.is_set(), "应先开始等待登录再展示二维码")
        self.client.events.append("render")
        self.rendered_urls.append(url)
        Path(path).write_bytes(b"fake PNG contents")

    def password_prompt(self):
        self.password_prompt_calls += 1
        return PASSWORD_SECRET

    async def run_login(self, **overrides):
        options = {
            "qr_file": self.qr_file,
            "timeout_seconds": 5,
            "render": self.render,
            "password_prompt": self.password_prompt,
            "log": self.logs.append,
        }
        options.update(overrides)
        return await qr_login.run_qr_login(self.client, **options)

    def assert_cleaned_up(self):
        self.assertFalse(self.qr_file.exists())
        self.assertEqual(self.client.disconnect_calls, 1)
        self.assertEqual(self.client.log_out_calls, 0)

    async def test_wait_starts_before_render_and_success_removes_generated_png(self):
        result = await self.run_login()
        self.assertIsInstance(result, qr_login.LoginResult)
        self.assertEqual(result.account_id, 123)
        self.assertEqual(result.username, "fixture_user")
        self.assertFalse(result.already_authorized)
        self.assertEqual(self.rendered_urls, [QR_SECRET])
        self.assertLess(self.client.events.index("wait_started"), self.client.events.index("render"))
        self.assertEqual(self.client.qr_login_calls, 1)
        self.assert_cleaned_up()

    async def test_first_expired_qr_checks_authorization_and_refreshes_then_succeeds(self):
        self.client.qr.expires = datetime.now(timezone.utc) + timedelta(seconds=0.5)
        self.client.qr.wait_effects = [asyncio.TimeoutError(), self.client.user]
        self.client.authorized_effects = [False, False]
        result = await self.run_login()
        self.assertFalse(result.already_authorized)
        self.assertEqual(self.client.qr.recreate_calls, 1)
        self.assertEqual(len(self.rendered_urls), 2)
        first_wait = self.client.events.index("wait_started")
        refreshed = self.client.events.index("recreate")
        self.assertIn("is_user_authorized", self.client.events[first_wait + 1:refreshed])
        self.assertTrue(all(0 < value <= 5 for value in self.client.qr.wait_timeouts))
        self.assert_cleaned_up()

    async def test_authorization_race_at_expiry_succeeds_without_refreshing(self):
        self.client.qr.wait_effects = [asyncio.TimeoutError()]
        self.client.authorized_effects = [False, True]
        result = await self.run_login()
        self.assertEqual(result.account_id, 123)
        self.assertFalse(result.already_authorized)
        self.assertEqual(self.client.qr.recreate_calls, 0)
        self.assert_cleaned_up()

    async def test_total_scan_timeout_cancels_wait_and_removes_generated_png(self):
        blocker = asyncio.Event()
        self.client.qr.wait_effects = [blocker.wait]
        with self.assertRaises(qr_login.QRLoginError):
            await self.run_login(timeout_seconds=0.05)
        self.assertTrue(self.client.qr.wait_cancelled)
        self.assert_cleaned_up()

    async def test_scan_deadline_bounds_connect_authorization_and_qr_creation_rpcs(self):
        for stage in ("connect", "is_user_authorized", "qr_login"):
            with self.subTest(stage=stage):
                self.client = FakeClient()
                self.client.stage_effects[stage] = asyncio.Event().wait
                with self.assertRaises(qr_login.QRLoginError):
                    await asyncio.wait_for(self.run_login(timeout_seconds=0.03), timeout=1)
                self.assertEqual(self.client.disconnect_calls, 1)
                self.assertEqual(self.client.log_out_calls, 0)
                self.assertEqual(self.rendered_urls, [])
                self.assertFalse(self.qr_file.exists())

    async def test_scan_deadline_bounds_qr_refresh_rpc(self):
        self.client.qr.wait_effects = [asyncio.TimeoutError()]
        self.client.qr.recreate_effect = asyncio.Event().wait
        with self.assertRaises(qr_login.QRLoginError):
            await asyncio.wait_for(self.run_login(timeout_seconds=0.05), timeout=1)
        self.assertEqual(self.client.qr.recreate_calls, 1)
        self.assert_cleaned_up()

    async def test_refresh_does_not_restart_total_scan_deadline(self):
        elapsed = [100.0]
        clock = SimpleNamespace(monotonic=lambda: elapsed[0])
        self.client.qr.wait_effects = [asyncio.TimeoutError()]

        async def advance_past_deadline():
            elapsed[0] += 6

        self.client.qr.recreate_effect = advance_past_deadline
        with patch.object(qr_login, "time", clock):
            with self.assertRaises(qr_login.QRLoginError):
                await self.run_login(timeout_seconds=5)
        self.assertEqual(self.client.qr.recreate_calls, 1)
        self.assertEqual(len(self.rendered_urls), 1)
        self.assert_cleaned_up()

    async def test_two_factor_password_completes_login_without_logging_secrets(self):
        self.client.qr.wait_effects = [SessionPasswordNeededError(request=None)]
        output = StringIO()
        with redirect_stdout(output):
            result = await self.run_login()
        self.assertEqual(result.account_id, 123)
        self.assertEqual(self.password_prompt_calls, 1)
        self.assertEqual(self.client.passwords, [PASSWORD_SECRET])
        combined = output.getvalue() + "\n".join(self.logs)
        for secret in (QR_SECRET, PASSWORD_SECRET, SESSION_SECRET):
            self.assertNotIn(secret, combined)
        self.assert_cleaned_up()

    async def test_wrong_two_factor_password_can_be_corrected(self):
        self.client.qr.wait_effects = [SessionPasswordNeededError(request=None)]
        self.client.sign_in_effects = [PasswordHashInvalidError(request=None), self.client.user]
        result = await self.run_login()
        self.assertEqual(result.account_id, 123)
        self.assertEqual(self.password_prompt_calls, 2)
        self.assertEqual(len(self.client.passwords), 2)
        self.assert_cleaned_up()

    async def test_initial_qr_creation_can_require_two_factor_password(self):
        self.client.stage_effects["qr_login"] = SessionPasswordNeededError(request=None)
        result = await self.run_login()
        self.assertEqual(result.account_id, 123)
        self.assertFalse(result.already_authorized)
        self.assertEqual(self.rendered_urls, [])
        self.assertEqual(self.client.passwords, [PASSWORD_SECRET])
        self.assert_cleaned_up()

    async def test_qr_refresh_can_require_two_factor_password(self):
        self.client.qr.wait_effects = [asyncio.TimeoutError()]
        self.client.qr.recreate_effect = SessionPasswordNeededError(request=None)
        result = await self.run_login()
        self.assertEqual(result.account_id, 123)
        self.assertFalse(result.already_authorized)
        self.assertEqual(self.client.qr.recreate_calls, 1)
        self.assertEqual(self.client.passwords, [PASSWORD_SECRET])
        self.assert_cleaned_up()

    async def test_qr_cleanup_permission_error_does_not_block_two_factor_login(self):
        self.client.qr.wait_effects = [SessionPasswordNeededError(request=None)]
        original_unlink = Path.unlink

        def refuse_qr_cleanup(path, *args, **kwargs):
            if path == self.qr_file:
                raise PermissionError(SESSION_SECRET)
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", refuse_qr_cleanup):
            result = await self.run_login()
        self.assertEqual(result.account_id, 123)
        self.assertEqual(self.client.passwords, [PASSWORD_SECRET])
        self.assertTrue(self.qr_file.exists())
        self.assertEqual(self.client.disconnect_calls, 1)
        self.assertEqual(self.client.log_out_calls, 0)
        self.assertTrue(any("手动删除" in message for message in self.logs))
        self.assertNotIn(SESSION_SECRET, "\n".join(self.logs))

    async def test_two_factor_input_time_does_not_consume_scan_deadline(self):
        elapsed = [100.0]
        clock = SimpleNamespace(monotonic=lambda: elapsed[0])
        self.client.qr.wait_effects = [SessionPasswordNeededError(request=None)]

        def slow_password_prompt():
            elapsed[0] += 600
            return self.password_prompt()

        with patch.object(qr_login, "time", clock):
            result = await self.run_login(timeout_seconds=5, password_prompt=slow_password_prompt)
        self.assertEqual(result.account_id, 123)
        self.assertEqual(self.client.passwords, [PASSWORD_SECRET])
        self.assert_cleaned_up()

    async def test_two_factor_sign_in_network_request_has_thirty_second_timeout(self):
        self.client.qr.wait_effects = [SessionPasswordNeededError(request=None)]
        self.client.stage_effects["sign_in"] = asyncio.Event().wait
        original_wait_for = asyncio.wait_for
        password_request_timeouts = []

        async def accelerated_wait_for(awaitable, timeout):
            if timeout == 30:
                password_request_timeouts.append(timeout)
                timeout = 0.01
            return await original_wait_for(awaitable, timeout=timeout)

        with patch.object(qr_login.asyncio, "wait_for", accelerated_wait_for):
            with self.assertRaises(qr_login.QRLoginError):
                await self.run_login()
        self.assertEqual(password_request_timeouts, [30])
        self.assertEqual(len(self.client.passwords), 1)
        self.assert_cleaned_up()

    async def test_three_wrong_password_attempts_stop_without_logging_password(self):
        self.client.qr.wait_effects = [SessionPasswordNeededError(request=None)]
        self.client.sign_in_effects = [PasswordHashInvalidError(request=None) for _ in range(3)]
        with self.assertRaises(qr_login.QRLoginError) as raised:
            await self.run_login()
        self.assertEqual(self.password_prompt_calls, 3)
        self.assertEqual(len(self.client.passwords), 3)
        self.assertNotIn(PASSWORD_SECRET, "\n".join(self.logs) + str(raised.exception))
        self.assert_cleaned_up()

    async def test_render_failure_removes_partial_png_and_cancels_wait(self):
        self.client.qr.wait_effects = [asyncio.Event().wait]

        def failed_render(url, path):
            self.render(url, path)
            raise RuntimeError(QR_SECRET)

        with self.assertRaises(RuntimeError):
            await self.run_login(render=failed_render)
        self.assertTrue(self.client.qr.wait_cancelled)
        self.assertNotIn(QR_SECRET, "\n".join(self.logs))
        self.assert_cleaned_up()

    async def test_cancellation_disconnects_and_cleans_generated_png(self):
        self.client.qr.wait_effects = [asyncio.Event().wait]
        displayed = asyncio.Event()

        def tracked_render(url, path):
            self.render(url, path)
            displayed.set()

        task = asyncio.create_task(self.run_login(render=tracked_render))
        await asyncio.wait_for(displayed.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.client.qr.wait_cancelled)
        self.assert_cleaned_up()

    async def test_disconnect_failure_still_removes_generated_png(self):
        self.client.stage_effects["disconnect"] = RuntimeError(SESSION_SECRET)
        try:
            await self.run_login()
        except qr_login.QRLoginError:
            pass
        self.assert_cleaned_up()
        self.assertNotIn(SESSION_SECRET, "\n".join(self.logs))

    async def test_already_authorized_reuses_session_without_qr_or_file_changes(self):
        self.client.authorized_effects = [True]
        self.qr_file.write_bytes(b"existing image unrelated to this login")
        candidate = self.root / "candidate.session"
        current = self.root / "current.session"
        candidate.write_bytes(b"candidate session fixture")
        current.write_bytes(b"current session fixture")
        expected = {path: path.read_bytes() for path in (self.qr_file, candidate, current)}
        self.client.session.filename = str(candidate)
        result = await self.run_login()
        self.assertTrue(result.already_authorized)
        self.assertEqual(result.account_id, 123)
        self.assertEqual(self.client.qr_login_calls, 0)
        self.assertEqual(self.rendered_urls, [])
        self.assertEqual(self.password_prompt_calls, 0)
        self.assertEqual(self.client.disconnect_calls, 1)
        self.assertEqual(self.client.log_out_calls, 0)
        for path, contents in expected.items():
            self.assertEqual(path.read_bytes(), contents)


@unittest.skipIf(qr_login is None, "二维码登录验证在包含 Telethon 的项目容器中运行")
class QRPasswordAndRenderingTests(unittest.TestCase):
    def test_noninteractive_terminal_rejects_password_without_calling_getpass(self):
        noninteractive_stdin = SimpleNamespace(isatty=lambda: False)
        with patch.object(qr_login.sys, "stdin", noninteractive_stdin), patch.object(
            qr_login.getpass, "getpass"
        ) as getpass:
            with self.assertRaises(qr_login.QRLoginError):
                qr_login.prompt_two_factor_password()
        getpass.assert_not_called()

    def test_getpass_warning_stops_before_password_echo_and_uses_fixed_error(self):
        interactive_stdin = SimpleNamespace(isatty=lambda: True)
        echoed_passwords = []

        def insecure_getpass(prompt):
            warnings.warn(PASSWORD_SECRET, qr_login.getpass.GetPassWarning)
            echoed_passwords.append(PASSWORD_SECRET)
            print(PASSWORD_SECRET)
            return PASSWORD_SECRET

        output = StringIO()
        errors = StringIO()
        with patch.object(qr_login.sys, "stdin", interactive_stdin), patch.object(
            qr_login.getpass, "getpass", insecure_getpass
        ), redirect_stdout(output), redirect_stderr(errors):
            with self.assertRaises(qr_login.QRLoginError) as raised:
                qr_login.prompt_two_factor_password()
        self.assertEqual(str(raised.exception), "当前终端无法隐藏密码输入，请改用交互终端运行。")
        self.assertEqual(echoed_passwords, [])
        self.assertNotIn(PASSWORD_SECRET, output.getvalue() + errors.getvalue())

    def test_real_renderer_creates_square_png_with_white_border_without_plaintext_url(self):
        from PIL import Image, ImageChops

        with tempfile.TemporaryDirectory() as directory:
            output_file = Path(directory) / "nested" / "telegram-login.png"
            output = StringIO()
            with redirect_stdout(output):
                qr_login.render_qr(QR_SECRET, output_file)
            self.assertTrue(output_file.is_file())
            self.assertFalse(output_file.with_name(output_file.name + ".tmp").exists())
            with Image.open(output_file) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.width, image.height)
                self.assertGreater(image.width, 0)
                rgb = image.convert("RGB")
                background = Image.new("RGB", rgb.size, (255, 255, 255))
                ink_bounds = ImageChops.difference(rgb, background).getbbox()
                self.assertIsNotNone(ink_bounds)
                self.assertGreater(ink_bounds[0], 0)
                self.assertGreater(ink_bounds[1], 0)
                self.assertLess(ink_bounds[2], image.width)
                self.assertLess(ink_bounds[3], image.height)
            self.assertNotIn(QR_SECRET, output.getvalue())


if __name__ == "__main__":
    unittest.main()
