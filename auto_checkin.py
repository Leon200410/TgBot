# -*- coding: utf-8 -*-
"""指定 Telegram 机器人的查岗关键词回复，不连接网络即可测试。"""

import asyncio
import json
import os
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class CheckinPrompt:
    number: str
    keyword: str
    window_minutes: int


_HEADER = re.compile(r"(?:🔔\s*)?查岗提醒\s*[（(]\s*编号\s*(\d{1,18})\s*[）)]")
_REQUEST = re.compile(r"(?m)^[ \t]*请在[ \t]*(\d{1,4})[ \t]*分钟内[ \t]*只回复下面的关键词[ \t]*[：:][ \t]*$")
_FOOTER = re.compile(r"(?m)^[ \t]*回复时限说明[ \t]*[：:][ \t]*$")
_CONFIRMATION = re.compile(r"编号[ \t]*(\d{1,18})[ \t]*[·•][ \t]*已记录")
_STATUSES = {"sending", "sent", "confirmed", "send_unknown", "expired"}


def parse_checkin_prompt(text: str) -> CheckinPrompt | None:
    """只接受截图中的模板，关键词区必须恰好有一个非空行。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    header = _HEADER.fullmatch(text.splitlines()[0].strip())
    requests = list(_REQUEST.finditer(text))
    footers = list(_FOOTER.finditer(text))
    if header is None or len(requests) != 1 or len(footers) != 1:
        return None
    request, footer = requests[0], footers[0]
    minutes = int(request.group(1))
    if minutes <= 0 or footer.start() <= request.end():
        return None
    lines = [line.strip() for line in text[request.end():footer.start()].splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    keyword = lines[0]
    if len(keyword) > 100 or any(unicodedata.category(ch).startswith("C") for ch in keyword):
        return None
    return CheckinPrompt(header.group(1), keyword, minutes)


def _confirmation_number(text: str) -> str | None:
    lines = text.strip().splitlines()
    if not lines or re.fullmatch(r"(?:✅\s*)?按时响应", lines[0].strip()) is None:
        return None
    matches = list(_CONFIRMATION.finditer(text))
    return matches[0].group(1) if len(matches) == 1 else None


class AutoCheckinResponder:
    def __init__(
        self,
        bot_id: int,
        state_file: Path,
        send_keyword: Callable[[str], Awaitable[object]],
        *,
        log: Callable[[str], None] = print,
    ):
        self.bot_id = bot_id
        self.state_file = Path(state_file)
        self._send_keyword = send_keyword
        self._log = log
        self._lock = asyncio.Lock()
        self._records: dict[str, dict] = {}
        self._ready = True
        try:
            if self.state_file.exists():
                state = json.loads(self.state_file.read_text(encoding="utf-8"))
                if (
                    not isinstance(state, dict)
                    or state.get("version") != 1
                    or state.get("bot_id") != bot_id
                    or not isinstance(state.get("records"), dict)
                ):
                    raise ValueError("状态文件格式或机器人 ID 不匹配")
                records = state["records"]
                if any(
                    re.fullmatch(r"\d{1,18}", number) is None
                    or not isinstance(record, dict)
                    or record.get("status") not in _STATUSES
                    for number, record in records.items()
                ):
                    raise ValueError("状态文件含无效记录")
                self._records = records
                uncertain = [number for number, record in records.items() if record["status"] in {"sending", "send_unknown"}]
                if uncertain:
                    self._log(f"[自动查岗] 有 {len(uncertain)} 条发送结果待确认，将跳过这些编号以免重复回复")
        except (OSError, ValueError, TypeError) as exc:
            self._ready = False
            self._log(f"[自动查岗] 状态读取失败，自动回复已停止: {type(exc).__name__}: {exc}")

    @property
    def ready(self) -> bool:
        return self._ready

    def _save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump({"version": 1, "bot_id": self.bot_id, "records": self._records}, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(self.state_file)

    def _save_or_stop(self) -> bool:
        try:
            self._save()
            return True
        except OSError as exc:
            self._ready = False
            self._log(f"[自动查岗] 状态保存失败，自动回复已停止: {type(exc).__name__}: {exc}")
            return False

    async def handle(self, event, *, now: datetime | None = None) -> bool:
        message = event.message
        if (
            not event.is_private
            or event.sender_id != self.bot_id
            or event.chat_id != self.bot_id
            or message.out
            or message.fwd_from is not None
            or message.via_bot_id is not None
        ):
            return False
        text = message.raw_text or ""
        confirmed_number = _confirmation_number(text)
        prompt = parse_checkin_prompt(text)
        if prompt is None and confirmed_number is None:
            if "查岗提醒" in text:
                self._log("[自动查岗] 收到查岗消息，但格式无法识别，未自动回复")
            return False

        async with self._lock:
            if not self._ready:
                return True
            if confirmed_number is not None:
                record = self._records.get(confirmed_number)
                if record is not None and record["status"] != "confirmed":
                    record["status"] = "confirmed"
                    if self._save_or_stop():
                        self._log(f"[自动查岗] 编号 {confirmed_number}: 已收到机器人“按时响应、已记录”确认")
                return True

            if prompt.number in self._records:
                self._log(f"[自动查岗] 编号 {prompt.number}: 已处理或发送结果待确认，跳过重复消息")
                return True
            current = now if now is not None else datetime.now(timezone.utc)
            date = message.date
            if not isinstance(date, datetime) or date.tzinfo is None or current.tzinfo is None:
                self._log(f"[自动查岗] 编号 {prompt.number}: 消息时间无效，未自动回复")
                return True
            age = (current - date).total_seconds()
            remaining = prompt.window_minutes * 60 - age
            started = time.monotonic()
            if age < -60 or remaining <= 0:
                self._log(f"[自动查岗] 编号 {prompt.number}: 已过回复期限或消息时间异常，未自动回复")
                return True

            # 先落盘，防止发送期间退出后重启再次发送。同一编号串行处理。
            record = {
                "status": "sending",
                "keyword": prompt.keyword,
                "prompt_message_id": message.id,
                "requested_at": date.isoformat(),
            }
            self._records[prompt.number] = record
            if not self._save_or_stop():
                return True
            remaining -= time.monotonic() - started
            if remaining <= 0:
                record["status"] = "expired"
                self._save_or_stop()
                self._log(f"[自动查岗] 编号 {prompt.number}: 处理期间已过回复期限，未自动回复")
                return True
            try:
                sent = await asyncio.wait_for(self._send_keyword(prompt.keyword), timeout=min(30, remaining))
            except Exception as exc:
                # 网络错误不能证明 Telegram 未收到，保留编号并等待回执或人工核对。
                record["status"] = "send_unknown"
                self._save_or_stop()
                self._log(f"[自动查岗] 编号 {prompt.number}: 发送结果无法确认，不自动重试: {type(exc).__name__}: {exc}")
                return True
            record["status"] = "sent"
            record["sent_message_id"] = getattr(sent, "id", None)
            self._save_or_stop()
            self._log(f"[自动查岗] 编号 {prompt.number}: 已发送关键词，等待机器人登记确认")
            return True
