"""在含 Telethon 的容器中验证接入；使用假的客户端，不连接 Telegram。"""

import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

try:
    import main
    from telethon.tl.types import Channel, Chat, ChatPhotoEmpty, User
except ModuleNotFoundError as exc:
    if exc.name != "telethon":
        raise
    main = None


@unittest.skipIf(main is None, "接入验证在包含 Telethon 的项目容器中运行")
class CheckinStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(main, "AUTO_CHECKIN_ENABLED", True))
        self.stack.enter_context(patch.object(main, "CHECKIN_BOT_USERNAME", "sy_agent003_bot"))
        self.stack.enter_context(patch.object(main, "CHECKIN_ALLOW_USER_TEST", False))
        self.stack.enter_context(patch.object(main, "SESSION_FILE", self.root / "tg_session"))
        self.output = StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.bot = User(id=123, bot=True, username="sy_agent003_bot")
        self.client = SimpleNamespace(
            get_entity=AsyncMock(return_value=self.bot),
            send_message=AsyncMock(return_value=SimpleNamespace(id=999)),
            add_event_handler=Mock(),
        )

    async def test_disabled_feature_does_not_resolve_or_register(self):
        with patch.object(main, "AUTO_CHECKIN_ENABLED", False):
            await main.start_auto_checkin(self.client, 456)
        self.client.get_entity.assert_not_awaited()
        self.client.add_event_handler.assert_not_called()

    async def test_missing_username_does_not_resolve_or_register(self):
        with patch.object(main, "CHECKIN_BOT_USERNAME", ""):
            await main.start_auto_checkin(self.client, 456)
        self.client.get_entity.assert_not_awaited()
        self.client.add_event_handler.assert_not_called()

    async def test_human_account_is_not_registered(self):
        self.bot.bot = False
        await main.start_auto_checkin(self.client, 456)
        self.client.add_event_handler.assert_not_called()
        self.client.send_message.assert_not_awaited()
        self.assertIn("CHECKIN_ALLOW_USER_TEST=true", self.output.getvalue())

    async def test_explicit_user_test_registers_only_the_resolved_human_account(self):
        self.bot = User(id=789, bot=False, username="Leon_Li451")
        self.client.get_entity.return_value = self.bot
        with patch.object(main, "CHECKIN_ALLOW_USER_TEST", True), patch.object(
            main, "CHECKIN_BOT_USERNAME", "Leon_Li451"
        ):
            await main.start_auto_checkin(self.client, 456)
        self.client.get_entity.assert_awaited_once_with("Leon_Li451")
        self.client.add_event_handler.assert_called_once()
        callback, builder = self.client.add_event_handler.call_args.args
        responder = callback.__self__
        self.assertEqual(responder.bot_id, 789)
        self.assertEqual(responder.state_file, self.root / "auto_checkin_456_789.json")
        self.assertEqual(builder.from_users, 789)
        self.assertTrue(builder.incoming)
        self.assertIn("指定私人号测试模式", self.output.getvalue())
        self.client.send_message.assert_not_awaited()
        await responder._send_keyword("苹果")
        self.client.send_message.assert_awaited_once_with(self.bot, "苹果", parse_mode=None)

    async def test_own_account_is_not_registered_even_when_user_test_is_enabled(self):
        self.bot = User(id=456, bot=False, username="my_account")
        self.client.get_entity.return_value = self.bot
        with patch.object(main, "CHECKIN_ALLOW_USER_TEST", True):
            await main.start_auto_checkin(self.client, 456)
        self.client.add_event_handler.assert_not_called()
        self.client.send_message.assert_not_awaited()
        self.assertIn("当前登录账号自身", self.output.getvalue())

    async def test_registered_user_test_callback_replies_to_screenshot_prompt(self):
        target = User(id=789, bot=False, username="Leon_Li451")
        self.client.get_entity.return_value = target
        with patch.object(main, "CHECKIN_ALLOW_USER_TEST", True), patch.object(
            main, "CHECKIN_BOT_USERNAME", "Leon_Li451"
        ):
            await main.start_auto_checkin(self.client, 456)
        callback, _ = self.client.add_event_handler.call_args.args
        prompt = """🔔 查岗提醒 (编号 50080)

嗨，立川 👋

查岗小助手来啦～

请在20分钟内只回复下面的关键词：

苹果

回复时限说明：
20分钟内回复：正常响应。
20-30分钟内回复：超时回复。
超过30分钟未正确回复：未响应。

祝工作顺利 🌷"""
        event = SimpleNamespace(
            is_private=True,
            sender_id=target.id,
            chat_id=target.id,
            message=SimpleNamespace(
                raw_text=prompt,
                date=datetime.now(timezone.utc),
                out=False,
                fwd_from=None,
                via_bot_id=None,
                id=900,
            ),
        )
        self.assertTrue(await callback(event))
        self.client.send_message.assert_awaited_once_with(target, "苹果", parse_mode=None)

    async def test_group_and_channel_are_not_registered_even_when_user_test_is_enabled(self):
        timestamp = datetime(2026, 10, 13, tzinfo=timezone.utc)
        targets = [
            Chat(
                id=321, title="test group", photo=ChatPhotoEmpty(),
                participants_count=1, date=timestamp, version=1,
            ),
            Channel(
                id=654, title="test channel", photo=ChatPhotoEmpty(),
                date=timestamp, broadcast=True,
            ),
        ]
        with patch.object(main, "CHECKIN_ALLOW_USER_TEST", True):
            for target in targets:
                with self.subTest(target=type(target).__name__):
                    self.client.get_entity.return_value = target
                    await main.start_auto_checkin(self.client, 456)
                    self.client.add_event_handler.assert_not_called()
                    self.client.send_message.assert_not_awaited()

    async def test_resolution_failure_keeps_existing_program_running(self):
        self.client.get_entity.side_effect = ValueError("无法解析目标")
        await main.start_auto_checkin(self.client, 456)
        self.client.add_event_handler.assert_not_called()

    async def test_corrupt_state_does_not_report_enabled_or_register(self):
        (self.root / "auto_checkin_456_123.json").write_text("broken", encoding="utf-8")
        await main.start_auto_checkin(self.client, 456)
        self.client.add_event_handler.assert_not_called()
        self.assertNotIn("已绑定", self.output.getvalue())

    async def test_bound_callback_sends_exact_plain_text_to_resolved_bot(self):
        await main.start_auto_checkin(self.client, 456)
        self.client.get_entity.assert_awaited_once_with("sy_agent003_bot")
        callback, builder = self.client.add_event_handler.call_args.args
        responder = callback.__self__
        self.assertEqual(responder.bot_id, 123)
        self.assertEqual(responder.state_file, self.root / "auto_checkin_456_123.json")
        self.assertTrue(builder.incoming)
        self.assertIsInstance(builder, main.events.NewMessage)
        await responder._send_keyword("**橙子**")
        self.client.send_message.assert_awaited_once_with(self.bot, "**橙子**", parse_mode=None)


if __name__ == "__main__":
    unittest.main()
