"""查岗自动回复的离线验证；不连接 Telegram，也不发送真实消息。"""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from auto_checkin import AutoCheckinResponder, CheckinPrompt, parse_checkin_prompt


NOW = datetime(2026, 10, 13, 4, 44, tzinfo=timezone.utc)
BOT_ID = 123
PROMPT = """🔔 查岗提醒（编号 50079）

嗨，立川 👋

查岗小助手来啦～

请在20分钟内只回复下面的关键词：

橙子

回复时限说明：
20分钟内回复：正常响应。
20-30分钟内回复：超时回复。
超过30分钟未正确回复：未响应。

祝工作顺利 🌷"""
CONFIRMATION = "✅ 按时响应\n编号 50079 · 已记录 · 感谢配合！"


def make_event(text=PROMPT, *, date=NOW, **overrides):
    message = SimpleNamespace(
        raw_text=text,
        date=date,
        out=False,
        fwd_from=None,
        via_bot_id=None,
        id=900,
    )
    event = SimpleNamespace(
        is_private=True,
        sender_id=BOT_ID,
        chat_id=BOT_ID,
        message=message,
    )
    for name, value in overrides.items():
        if hasattr(message, name):
            setattr(message, name, value)
        else:
            setattr(event, name, value)
    return event


class ParseCheckinPromptTests(unittest.TestCase):
    def test_screenshot_template_extracts_only_keyword_and_number(self):
        self.assertEqual(
            parse_checkin_prompt(PROMPT),
            CheckinPrompt(number="50079", keyword="橙子", window_minutes=20),
        )

    def test_crlf_and_surrounding_blank_lines(self):
        self.assertEqual(
            parse_checkin_prompt("\r\n" + PROMPT.replace("\n", "\r\n") + "\r\n"),
            CheckinPrompt(number="50079", keyword="橙子", window_minutes=20),
        )

    def test_confirmation_is_not_a_prompt(self):
        self.assertIsNone(parse_checkin_prompt(CONFIRMATION))

    def test_ordinary_text_and_nontext_are_ignored(self):
        for text in ("", "橙子", "请回复橙子", "查岗记录：本月暂无查岗记录。"):
            with self.subTest(text=text):
                self.assertIsNone(parse_checkin_prompt(text))

    def test_header_must_be_first_nonempty_line(self):
        self.assertIsNone(parse_checkin_prompt("同事转来的内容：\n" + PROMPT))

    def test_missing_required_sections_are_ignored(self):
        for section in (
            "🔔 查岗提醒（编号 50079）",
            "请在20分钟内只回复下面的关键词：",
            "回复时限说明：",
        ):
            with self.subTest(section=section):
                self.assertIsNone(parse_checkin_prompt(PROMPT.replace(section, "")))

    def test_keyword_section_requires_one_nonempty_line(self):
        for keyword in ("", "橙子\n苹果"):
            with self.subTest(keyword=keyword):
                self.assertIsNone(parse_checkin_prompt(PROMPT.replace("\n橙子\n", f"\n{keyword}\n")))

    def test_keyword_length_limit(self):
        self.assertIsNotNone(parse_checkin_prompt(PROMPT.replace("橙子", "果" * 100)))
        self.assertIsNone(parse_checkin_prompt(PROMPT.replace("橙子", "果" * 101)))

    def test_missing_or_nonnumeric_number_is_ignored(self):
        for header in ("🔔 查岗提醒", "🔔 查岗提醒（编号 abc）"):
            with self.subTest(header=header):
                self.assertIsNone(parse_checkin_prompt(PROMPT.replace("🔔 查岗提醒（编号 50079）", header)))


class AutoCheckinResponderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_file = Path(self.temp_dir.name) / "checkin-state.json"
        self.sent = []
        self.logs = []

    async def fake_send(self, keyword):
        self.sent.append(keyword)
        await asyncio.sleep(0)
        return SimpleNamespace(id=901)

    def responder(self, send=None, state_file=None):
        return AutoCheckinResponder(
            BOT_ID,
            state_file if state_file is not None else self.state_file,
            send if send is not None else self.fake_send,
            log=self.logs.append,
        )

    def state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def write_state(self, *, status="sent", bot_id=BOT_ID, version=1):
        self.state_file.write_text(
            json.dumps({
                "version": version,
                "bot_id": bot_id,
                "records": {"50079": {"status": status}},
            }),
            encoding="utf-8",
        )

    async def test_fresh_prompt_sends_only_keyword_and_records_sent(self):
        handled = await self.responder().handle(make_event(), now=NOW)
        self.assertTrue(handled)
        self.assertEqual(self.sent, ["橙子"])
        state = self.state()
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["bot_id"], BOT_ID)
        self.assertEqual(state["records"]["50079"]["status"], "sent")

    async def test_sending_state_is_persisted_before_send(self):
        async def inspect_send(keyword):
            self.assertEqual(self.state()["records"]["50079"]["status"], "sending")
            self.sent.append(keyword)
            return SimpleNamespace(id=901)

        self.assertTrue(await self.responder(inspect_send).handle(make_event(), now=NOW))
        self.assertEqual(self.sent, ["橙子"])

    async def test_wrong_source_and_message_types_are_ignored(self):
        variants = (
            {"sender_id": 456},
            {"chat_id": 456},
            {"is_private": False},
            {"out": True},
            {"fwd_from": SimpleNamespace(from_id=BOT_ID)},
            {"via_bot_id": 456},
        )
        responder = self.responder()
        for changes in variants:
            with self.subTest(changes=changes):
                self.assertFalse(await responder.handle(make_event(**changes), now=NOW))
        self.assertEqual(self.sent, [])
        self.assertFalse(self.state_file.exists())

    async def test_unrelated_bot_message_is_ignored(self):
        self.assertFalse(await self.responder().handle(make_event("本月暂无查岗记录。"), now=NOW))
        self.assertEqual(self.sent, [])

    async def test_nineteen_minutes_fifty_nine_seconds_can_be_sent(self):
        self.assertTrue(await self.responder().handle(make_event(date=NOW - timedelta(minutes=20) + timedelta(seconds=1)), now=NOW))
        self.assertEqual(self.sent, ["橙子"])

    async def test_exactly_twenty_minutes_is_expired(self):
        self.assertTrue(await self.responder().handle(make_event(date=NOW - timedelta(minutes=20)), now=NOW))
        self.assertEqual(self.sent, [])

    async def test_deadline_reached_during_state_write_prevents_send(self):
        responder = self.responder()
        clock = SimpleNamespace(monotonic=Mock(side_effect=[1000.0, 1001.0]))
        event = make_event(date=NOW - timedelta(minutes=20) + timedelta(seconds=1))
        # 替换模块的 time 引用，保留 asyncio 自身使用的真实单调时钟。
        with patch("auto_checkin.time", clock):
            self.assertTrue(await responder.handle(event, now=NOW))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state()["records"]["50079"]["status"], "expired")

    async def test_future_more_than_sixty_seconds_is_rejected(self):
        self.assertTrue(await self.responder().handle(make_event(date=NOW + timedelta(seconds=61)), now=NOW))
        self.assertEqual(self.sent, [])

    async def test_small_clock_difference_is_allowed(self):
        self.assertTrue(await self.responder().handle(make_event(date=NOW + timedelta(seconds=60)), now=NOW))
        self.assertEqual(self.sent, ["橙子"])

    async def test_concurrent_duplicate_number_sends_once(self):
        responder = self.responder()
        handled = await asyncio.gather(*(responder.handle(make_event(id=900 + i), now=NOW) for i in range(8)))
        self.assertEqual(handled, [True] * 8)
        self.assertEqual(self.sent, ["橙子"])

    async def test_persisted_number_is_not_resent_after_restart(self):
        await self.responder().handle(make_event(), now=NOW)
        self.assertTrue(await self.responder().handle(make_event(id=910), now=NOW))
        self.assertEqual(self.sent, ["橙子"])

    async def test_every_existing_status_prevents_resend(self):
        for status in ("sending", "sent", "send_unknown", "confirmed", "expired"):
            with self.subTest(status=status):
                self.write_state(status=status)
                self.assertTrue(await self.responder().handle(make_event(), now=NOW))
        self.assertEqual(self.sent, [])

    async def test_send_exception_is_recorded_and_not_retried(self):
        async def failed_send(keyword):
            self.sent.append(keyword)
            raise RuntimeError("模拟发送失败")

        self.assertTrue(await self.responder(failed_send).handle(make_event(), now=NOW))
        self.assertEqual(self.state()["records"]["50079"]["status"], "send_unknown")
        self.assertTrue(await self.responder().handle(make_event(id=910), now=NOW))
        self.assertEqual(self.sent, ["橙子"])

    async def test_confirmation_updates_existing_record_without_sending(self):
        responder = self.responder()
        await responder.handle(make_event(), now=NOW)
        self.assertTrue(await responder.handle(make_event(CONFIRMATION, id=902), now=NOW))
        self.assertEqual(self.state()["records"]["50079"]["status"], "confirmed")
        self.assertEqual(self.sent, ["橙子"])

    async def test_confirmation_from_wrong_sender_does_not_update_record(self):
        self.write_state()
        self.assertFalse(await self.responder().handle(make_event(CONFIRMATION, sender_id=456), now=NOW))
        self.assertEqual(self.state()["records"]["50079"]["status"], "sent")
        self.assertEqual(self.sent, [])

    async def test_corrupt_or_incompatible_state_fails_closed(self):
        states = (
            "{损坏",
            json.dumps({"version": 1, "bot_id": 456, "records": {}}),
            json.dumps({"version": 999, "bot_id": BOT_ID, "records": {}}),
        )
        for content in states:
            with self.subTest(content=content):
                self.state_file.write_text(content, encoding="utf-8")
                responder = self.responder()
                self.assertFalse(responder.ready)
                self.assertTrue(await responder.handle(make_event(), now=NOW))
        self.assertEqual(self.sent, [])

    async def test_unwritable_state_path_prevents_send(self):
        blocker = Path(self.temp_dir.name) / "not-a-directory"
        blocker.write_text("阻止创建子目录", encoding="utf-8")
        responder = self.responder(state_file=blocker / "state.json")
        self.assertTrue(responder.ready)
        self.assertTrue(await responder.handle(make_event(), now=NOW))
        self.assertFalse(responder.ready)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
