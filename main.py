# -*- coding: utf-8 -*-
"""
Telegram 消息语音播报 + 保持在线(在 Docker 容器内运行)

- 用你自己的账号登录,登录状态(session)保存在 /data 卷里,重启容器不用重新登录
- 在 ONLINE_HOURS 指定的时段内,每 45 秒向 Telegram 上报一次在线状态
- 收到私聊消息、或在群里被@时,把播报文本发给宿主机上的语音服务(tts_server.py)朗读
- CHATLOG_CHATS 点名的群或私聊,消息追加写进 /chatlog 下的文本文件(只读取,不发送、不标已读)
- AUTO_CHECKIN_ENABLED 开启后,只向指定查岗机器人的新私聊提醒回复关键词

配置全部来自环境变量(见 docker-compose.yml 和 .env):
  API_ID / API_HASH   在 https://my.telegram.org 申请
  ONLINE_HOURS        always=全天在线;9:00-23:00=指定时段;留空=不保持在线
  TTS_URL             宿主机语音服务地址
  CHATLOG_CHATS       要记录的群名(私聊写对方的显示名),多个用英文逗号隔开;留空=不记录
"""
import asyncio
import json
import os
import queue
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events, functions
from telethon.errors import FloodWaitError
from telethon.tl.types import User

from auto_checkin import AutoCheckinResponder

API_ID = os.environ.get("API_ID", "").strip()
API_HASH = os.environ.get("API_HASH", "").strip()
ONLINE_HOURS = os.environ.get("ONLINE_HOURS", "").strip()
WEEKDAY_ONLINE_HOURS = os.environ.get("WEEKDAY_ONLINE_HOURS", "").strip()
SATURDAY_ONLINE_HOURS = os.environ.get("SATURDAY_ONLINE_HOURS", "").strip()
SUNDAY_ONLINE_HOURS = os.environ.get("SUNDAY_ONLINE_HOURS", "").strip()
TTS_URL = os.environ.get("TTS_URL", "http://host.docker.internal:8929/speak").strip()
DEVICE_MODEL = os.environ.get("DEVICE_MODEL", "Z880 MB").strip()
SYSTEM_VERSION = os.environ.get("SYSTEM_VERSION", "Windows 11").strip()
APP_VERSION = os.environ.get("APP_VERSION", "1.0").strip()
ONLINE_PING_SECONDS = max(10, int(os.environ.get("ONLINE_PING_SECONDS", "20").strip() or "20"))
ONLINE_ACTIVITY_PING_SECONDS = max(
    ONLINE_PING_SECONDS,
    int(os.environ.get("ONLINE_ACTIVITY_PING_SECONDS", "240").strip() or "240"),
)
ONLINE_REQUEST_TIMEOUT = 30  # 秒。Telethon 请求默认不超时,网络半死时会挂住整个保活循环
HEARTBEAT_LOG_SECONDS = 1800  # 秒。心跳正常时每隔这么久打一条确认日志,便于事后排查
SESSION_FILE = Path("/data") / "tg_session"
CHATLOG_CHATS = [s.strip() for s in os.environ.get("CHATLOG_CHATS", "").split(",") if s.strip()]
CHATLOG_DIR = Path(os.environ.get("CHATLOG_DIR", "/chatlog").strip() or "/chatlog")
CHATLOG_BACKFILL = int(os.environ.get("CHATLOG_BACKFILL", "300").strip() or "300")  # 首次记录时补多少条历史
CHATLOG_GAP_LIMIT = 2000  # 重启后补「上次记到哪」之后的消息,最多补这么多条
CHATLOG_BACKFILL_IMAGES = 40  # 补历史时只下载最近这么多张图片,新消息的图片都下载
CHATLOG_MAX_IMAGE_BYTES = 10 * 1024 * 1024
AUTO_CHECKIN_ENABLED = os.environ.get("AUTO_CHECKIN_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
CHECKIN_BOT_USERNAME = os.environ.get("CHECKIN_BOT_USERNAME", "").strip().lstrip("@")
CHECKIN_ALLOW_USER_TEST = os.environ.get("CHECKIN_ALLOW_USER_TEST", "false").strip().lower() in {"1", "true", "yes", "on"}


# ---------- 语音播报:独立线程排队发送给宿主机语音服务,不阻塞消息接收 ----------
_tts_queue: queue.Queue = queue.Queue()


def _tts_worker():
    while True:
        text = _tts_queue.get()
        data = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            TTS_URL, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[语音] 无法连接宿主机语音服务 {TTS_URL} : {e}")
            print("[语音] 请确认 Windows 上的 tts_server.bat 正在运行")


def speak(text: str):
    _tts_queue.put(text)


def display_name(entity) -> str:
    if entity is None:
        return "未知联系人"
    name = getattr(entity, "title", None)  # 群、频道用 title
    if not name:
        first = getattr(entity, "first_name", "") or ""
        last = getattr(entity, "last_name", "") or ""
        name = (first + " " + last).strip()
    return name or "未知联系人"


# ---------- 群聊记录:点名的群的消息追加写进本地文本文件(只读取,不发送、不标已读) ----------
_chatlog_lock = asyncio.Lock()
_chatlog_last_id: dict = {}  # 群 id -> 已记录到的最后一条消息 id,同时存在 state.json 里
_chatlog_pending: dict = {}  # 群 id -> 补历史期间先攒着的新消息;群 id 不在这里=已补完


def _chatlog_name(title: str) -> str:
    return "".join("_" if ch in '\\/:*?"<>|' else ch for ch in title).strip() or "未命名"


def _chatlog_is_image(message) -> bool:
    if message.sticker or message.file is None:
        return False
    if (message.file.size or 0) > CHATLOG_MAX_IMAGE_BYTES:
        return False
    return bool(message.photo) or (message.file.mime_type or "").startswith("image/")


async def _chatlog_write(message, chat_id: int, title: str, download_image: bool = True):
    async with _chatlog_lock:
        if message.id <= _chatlog_last_id.get(chat_id, 0):
            return
        name = _chatlog_name(title)
        line = f"[#{message.id} {message.date.astimezone():%Y-%m-%d %H:%M}] "
        line += f"{display_name(await message.get_sender())}:"
        reply_id = getattr(message.reply_to, "reply_to_msg_id", None)
        if reply_id:
            line += f" (回复 #{reply_id})"
        if message.forward is not None:
            line += " (转发)"
        text = (message.raw_text or "").strip().replace("\n", "\n    ")
        if text:
            line += " " + text
        if message.photo:
            line += " 〔图片〕"
        elif message.file is not None:
            line += f" 〔文件 {message.file.name or message.file.mime_type or ''}〕"
        if download_image and _chatlog_is_image(message):
            media_dir = CHATLOG_DIR / "media" / name
            media_dir.mkdir(parents=True, exist_ok=True)
            try:
                path = await message.download_media(file=str(media_dir / str(message.id)))
                if path:
                    line += f" → media/{name}/{Path(path).name}"
            except Exception as e:
                line += f" (图片下载失败: {type(e).__name__})"
        with open(CHATLOG_DIR / f"{name}.txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _chatlog_last_id[chat_id] = message.id
        (CHATLOG_DIR / "state.json").write_text(
            json.dumps({str(k): v for k, v in _chatlog_last_id.items()}), encoding="utf-8"
        )


async def _chatlog_backfill(client, chat_id: int, title: str):
    last_id = _chatlog_last_id.get(chat_id, 0)
    messages = [
        m
        async for m in client.iter_messages(
            chat_id, limit=CHATLOG_GAP_LIMIT if last_id else CHATLOG_BACKFILL, min_id=last_id
        )
    ]  # 新 -> 旧
    recent_images = {m.id for m in [m for m in messages if _chatlog_is_image(m)][:CHATLOG_BACKFILL_IMAGES]}
    for m in reversed(messages):
        await _chatlog_write(m, chat_id, title, download_image=m.id in recent_images)
    return len(messages)


async def start_chat_logging(client):
    CHATLOG_DIR.mkdir(parents=True, exist_ok=True)
    state_file = CHATLOG_DIR / "state.json"
    if state_file.exists():
        _chatlog_last_id.update({int(k): v for k, v in json.loads(state_file.read_text("utf-8")).items()})

    try:
        all_dialogs = [d async for d in client.iter_dialogs()]
    except Exception as e:
        print(f"[群聊记录] 读取群列表失败({type(e).__name__}: {e}),本次不记录")
        return
    # 先在群/频道里找;群里没有这个名字,再到私聊里找(私聊写对方的显示名),免得私聊抢了群的名字
    pools = (
        [d for d in all_dialogs if d.is_group or d.is_channel],
        [d for d in all_dialogs if d.is_user],
    )
    targets = {}
    for wanted in CHATLOG_CHATS:
        # 先按名字完全一致找,找不到再按「包含」找;对不上或对上多个都不记,免得记错
        matched = []
        for dialogs in pools:
            matched = [d for d in dialogs if d.name.strip() == wanted] or [d for d in dialogs if wanted in d.name]
            if matched:
                break
        if len(matched) == 1:
            targets[matched[0].id] = matched[0].name
        else:
            names = "、".join(d.name for d in matched) or "无"
            print(f"[群聊记录] 「{wanted}」对上了 {len(matched)} 个会话({names}),请把 CHATLOG_CHATS 写成完整名字")
    if not targets:
        return
    for chat_id in targets:
        _chatlog_pending[chat_id] = []

    @client.on(events.NewMessage(chats=list(targets)))
    async def on_logged_chat_message(event):
        pending = _chatlog_pending.get(event.chat_id)
        if pending is not None:  # 这个群还在补历史,先攒着,补完按顺序写
            pending.append(event.message)
            return
        await _chatlog_write(event.message, event.chat_id, targets[event.chat_id])

    for chat_id, title in targets.items():
        try:
            count = await _chatlog_backfill(client, chat_id, title)
            print(f"[群聊记录] {title}:补了 {count} 条,之后的新消息持续写入 {_chatlog_name(title)}.txt")
        except Exception as e:
            print(f"[群聊记录] {title}:补历史失败({type(e).__name__}: {e}),新消息照常记录")
        for m in sorted(_chatlog_pending.pop(chat_id), key=lambda m: m.id):
            await _chatlog_write(m, chat_id, title)


# ---------- 保持在线:在设定时段内定时向 Telegram 上报在线状态 ----------
def _parse_hm(s: str) -> int:
    """'9:30' -> 当天第几分钟。也接受纯小时如 '9'"""
    if ":" in s:
        h, m = s.split(":")
    else:
        h, m = s, "0"
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(s)
    return h * 60 + m


def parse_time_ranges(s: str, *, var_name: str):
    """返回 None、'always' 或 [(起始分钟, 结束分钟), ...]"""
    s = (s or "").strip().lower()
    if not s:
        return None
    if s in ("always", "全天"):
        return "always"
    try:
        ranges = []
        for part in s.split(","):
            start_s, end_s = part.split("-")
            ranges.append((_parse_hm(start_s.strip()), _parse_hm(end_s.strip())))
        return ranges
    except ValueError:
        print(f"{var_name} 的格式不对:{s}")
        print("可填 always(全天)、留空(不启用),或时间段如 9:00-23:00,多段用英文逗号隔开")
        sys.exit(1)


def parse_online_schedule():
    """返回 'always'、None、[(起始分钟, 结束分钟), ...] 或 {weekday: ranges}"""
    if WEEKDAY_ONLINE_HOURS or SATURDAY_ONLINE_HOURS or SUNDAY_ONLINE_HOURS:
        weekday_ranges = parse_time_ranges(WEEKDAY_ONLINE_HOURS, var_name="WEEKDAY_ONLINE_HOURS")
        saturday_ranges = parse_time_ranges(SATURDAY_ONLINE_HOURS, var_name="SATURDAY_ONLINE_HOURS")
        sunday_ranges = parse_time_ranges(SUNDAY_ONLINE_HOURS, var_name="SUNDAY_ONLINE_HOURS")
        schedule = {}
        for weekday in range(5):
            if weekday_ranges:
                schedule[weekday] = weekday_ranges
        if saturday_ranges:
            schedule[5] = saturday_ranges
        if sunday_ranges:
            schedule[6] = sunday_ranges
        return schedule or None
    return parse_time_ranges(ONLINE_HOURS, var_name="ONLINE_HOURS")


def _in_ranges(ranges, minute_of_day: int) -> bool:
    if ranges == "always":
        return True
    for start, end in ranges:
        if start <= end:
            if start <= minute_of_day < end:
                return True
        elif minute_of_day >= start or minute_of_day < end:  # 跨午夜,如 23:00-7:00
            return True
    return False


def should_keep_online(schedule, now: datetime) -> bool:
    if schedule == "always":
        return True
    if not schedule:
        return False
    minute_of_day = now.hour * 60 + now.minute
    if isinstance(schedule, dict):
        ranges = schedule.get(now.weekday())
        return _in_ranges(ranges, minute_of_day) if ranges else False
    return _in_ranges(schedule, minute_of_day)


def _format_ranges(ranges) -> str:
    if ranges == "always":
        return "全天"
    return ",".join(f"{s // 60}:{s % 60:02d}-{e // 60}:{e % 60:02d}" for s, e in ranges)


def describe_online_schedule(schedule) -> str:
    if schedule == "always":
        return "全天 24 小时"
    if not schedule:
        return "未启用(ONLINE_HOURS 为空)"
    if isinstance(schedule, dict):
        names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        parts = []
        for weekday in range(7):
            ranges = schedule.get(weekday)
            if ranges:
                parts.append(f"{names[weekday]} {_format_ranges(ranges)}")
        return "；".join(parts) if parts else "未启用"
    return f"每天 {_format_ranges(schedule)}"


async def touch_saved_messages(client):
    # 单纯 updateStatus 在部分账号/客户端组合下不够稳,这里定期做一次无痕活动 ping。
    msg = await client.send_message("me", "\u2063", silent=True)
    await client.delete_messages("me", msg.id)


async def keep_online(client, schedule):
    online = False
    last_activity_ping = 0.0
    activity_blocked_until = 0.0  # 活动 ping 被限流时,恢复执行的时间点
    last_heartbeat_log = 0.0
    while True:
        now = datetime.now()
        want = should_keep_online(schedule, now)

        # 心跳:上报在线/离线状态。失败只影响本轮,不影响下面的活动 ping。
        try:
            if want:
                await asyncio.wait_for(
                    client(functions.account.UpdateStatusRequest(offline=False)),
                    ONLINE_REQUEST_TIMEOUT,
                )
                if not online:
                    print(f"[{now:%H:%M:%S}] 进入保持在线时段,账号对外显示为在线")
                    last_heartbeat_log = time.monotonic()
                elif time.monotonic() - last_heartbeat_log >= HEARTBEAT_LOG_SECONDS:
                    print(f"[{now:%H:%M:%S}] 在线心跳正常(每 {ONLINE_PING_SECONDS} 秒上报一次)")
                    last_heartbeat_log = time.monotonic()
                online = True
            elif online:
                await asyncio.wait_for(
                    client(functions.account.UpdateStatusRequest(offline=True)),
                    ONLINE_REQUEST_TIMEOUT,
                )
                print(f"[{now:%H:%M:%S}] 离开保持在线时段,恢复正常状态")
                online = False
                last_activity_ping = 0.0
        except FloodWaitError as e:
            print(f"[{now:%H:%M:%S}] [在线] 状态上报被限流,等待 {e.seconds} 秒后继续")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"[{now:%H:%M:%S}] [在线] 状态上报失败: {type(e).__name__}: {e}")

        # 活动 ping:单独处理,被限流或失败都只跳过自己,心跳照常。
        if (
            want
            and online
            and time.monotonic() >= activity_blocked_until
            and time.monotonic() - last_activity_ping >= ONLINE_ACTIVITY_PING_SECONDS
        ):
            try:
                await asyncio.wait_for(touch_saved_messages(client), ONLINE_REQUEST_TIMEOUT)
                last_activity_ping = time.monotonic()
                print(f"[{now:%H:%M:%S}] 已执行保活 ping")
            except FloodWaitError as e:
                activity_blocked_until = time.monotonic() + e.seconds
                print(f"[{now:%H:%M:%S}] [在线] 保活 ping 被限流 {e.seconds} 秒,期间仅维持心跳")
            except Exception as e:
                print(f"[{now:%H:%M:%S}] [在线] 保活 ping 失败: {type(e).__name__}: {e}")

        await asyncio.sleep(ONLINE_PING_SECONDS)


async def start_auto_checkin(client, account_id: int):
    if not AUTO_CHECKIN_ENABLED:
        print("自动查岗:未启用(AUTO_CHECKIN_ENABLED=false)")
        return
    if not CHECKIN_BOT_USERNAME:
        print("[自动查岗] 缺少 CHECKIN_BOT_USERNAME，未启用自动回复")
        return
    try:
        target = await asyncio.wait_for(client.get_entity(CHECKIN_BOT_USERNAME), ONLINE_REQUEST_TIMEOUT)
        if not isinstance(target, User):
            print(f"[自动查岗] @{CHECKIN_BOT_USERNAME} 不是可私聊的用户账号，未启用自动回复")
            return
        if target.id == account_id:
            print("[自动查岗] 目标是当前登录账号自身，请使用另一个私人号测试，未启用自动回复")
            return
        if not target.bot and not CHECKIN_ALLOW_USER_TEST:
            print(f"[自动查岗] @{CHECKIN_BOT_USERNAME} 不是机器人账号，未启用自动回复；私人号测试需设置 CHECKIN_ALLOW_USER_TEST=true")
            return
    except Exception as exc:
        print(f"[自动查岗] 无法确认机器人身份，未启用自动回复: {type(exc).__name__}: {exc}")
        return

    async def send_keyword(keyword: str):
        # 不加前缀、表情或引用，也不让 Markdown 改写关键词。
        return await client.send_message(target, keyword, parse_mode=None)

    responder = AutoCheckinResponder(
        target.id,
        SESSION_FILE.parent / f"auto_checkin_{account_id}_{target.id}.json",
        send_keyword,
    )
    if not responder.ready:
        return
    client.add_event_handler(responder.handle, events.NewMessage(incoming=True, from_users=target.id))
    mode = "机器人模式" if target.bot else "指定私人号测试模式"
    print(f"自动查岗:已绑定 @{CHECKIN_BOT_USERNAME} (ID={target.id})，{mode}，只处理新的私聊查岗提醒")


async def amain():
    if not API_ID.isdigit() or not API_HASH:
        print("缺少 API_ID / API_HASH 环境变量。")
        print("请把 .env.example 复制为 .env,填入 https://my.telegram.org 申请的值。")
        sys.exit(1)
    online_schedule = parse_online_schedule()
    threading.Thread(target=_tts_worker, daemon=True).start()

    client = TelegramClient(
        str(SESSION_FILE),
        int(API_ID),
        API_HASH,
        # 下面三项决定 Telegram「活跃会话」列表里这个登录显示成什么
        device_model=DEVICE_MODEL,
        system_version=SYSTEM_VERSION,
        app_version=APP_VERSION,
    )

    print(
        f"当前会话标识: device_model={DEVICE_MODEL} | "
        f"system_version={SYSTEM_VERSION} | app_version={APP_VERSION}"
    )
    @client.on(events.NewMessage(incoming=True))
    async def on_message(event):
        msg = event.message
        # 播报只念来源不念内容;完整内容记录在日志里(docker compose logs 可查)
        preview = (msg.raw_text or "").strip().replace("\n", " ")[:80] or "<非文本消息>"
        if event.is_private:
            sender = display_name(await event.get_sender())
            print(f"[{datetime.now():%H:%M:%S}] 私聊 {sender}:{preview}")
            speak(f"收到 {sender} 的消息")
        elif msg.mentioned:  # 群或频道里被@,或有人回复了你的消息
            sender = display_name(await event.get_sender())
            chat = display_name(await event.get_chat())
            print(f"[{datetime.now():%H:%M:%S}] @提及 {chat} | {sender}:{preview}")
            speak(f"收到群聊 {chat} 的艾特")

    print("正在连接 Telegram...")
    await client.connect()
    if not await client.is_user_authorized() and not sys.stdin.isatty():
        print("尚未登录。请先在宿主机交互式地完成首次登录:")
        print("  docker compose run --rm tgbot")
        print("按提示输入手机号(如 +8613800138000)和验证码,之后再 docker compose up -d")
        sys.exit(1)
    await client.start()

    me = await client.get_me()
    print(f"已登录:{display_name(me)} (@{me.username or '无用户名'})")
    await start_auto_checkin(client, me.id)
    if online_schedule:
        print(f"保持在线:{describe_online_schedule(online_schedule)} (容器时区 TZ={os.environ.get('TZ', '未设置')})")
        print(f"保活心跳:{ONLINE_PING_SECONDS}秒,活动 ping:{ONLINE_ACTIVITY_PING_SECONDS}秒")
        keep_task = asyncio.create_task(keep_online(client, online_schedule))  # noqa: F841 持有引用,防止任务被回收
    else:
        print("保持在线:未启用")
    if CHATLOG_CHATS:
        print(f"群聊记录:{'、'.join(CHATLOG_CHATS)} -> {CHATLOG_DIR}")
        chatlog_task = asyncio.create_task(start_chat_logging(client))  # noqa: F841 持有引用,防止任务被回收
    else:
        print("群聊记录:未启用(CHATLOG_CHATS 为空)")
    print("收到私聊或群内@你时,会推送到宿主机语音播报。")
    print(f"[{datetime.now():%H:%M:%S}] Telegram 消息播报已启动")
    speak("Telegram 消息播报已启动")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\n已退出。")
