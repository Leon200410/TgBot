# -*- coding: utf-8 -*-
"""
宿主机语音服务(在 Windows 上直接运行,不进 Docker)

接收 Docker 容器 POST 到 /speak 的文本,排队朗读:
- 优先用 edge-tts 微软在线自然语音(需联网,音色自然)
- 失败时自动回退到 Windows 系统自带语音,保证不哑火

用法:双击 tts_server.bat,或 python tts_server.py [端口],默认端口 8929。
依赖:pip install pywin32 edge-tts
"""
import ctypes
import json
import os
import queue
import sys
import tempfile
import threading
import wave
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import winsound

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8929

# 音色:改这个值就能换,改完重启本服务生效。
# 完整列表:命令行运行  python -m edge_tts --list-voices
#   zh-CN-XiaoxiaoNeural   女声,自然亲切(默认)
#   zh-CN-XiaoyiNeural     女声,活泼
#   zh-CN-YunxiNeural      男声,阳光少年
#   zh-CN-YunjianNeural    男声,浑厚
#   zh-CN-YunyangNeural    男声,新闻播音腔
VOICE = "zh-CN-XiaoxiaoNeural"
# 有些 Windows 机器第一次出声会慢半拍,给每次播报前垫一小段静音可避免开头被吃掉。
LEAD_IN_MS = 300
# 每条播报默认重复次数。
REPEAT_TIMES = 2

try:
    import edge_tts
except ImportError:
    edge_tts = None

_queue: queue.Queue = queue.Queue()
_SILENCE_WAV = Path(tempfile.gettempdir()) / "tgbot_tts_silence.wav"


def _ensure_silence_wav(path: Path, duration_ms: int):
    if path.exists():
        return
    sample_rate = 16000
    frame_count = max(1, sample_rate * duration_ms // 1000)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * frame_count)


def _play_lead_in_silence():
    if LEAD_IN_MS <= 0:
        return
    _ensure_silence_wav(_SILENCE_WAV, LEAD_IN_MS)
    winsound.PlaySound(str(_SILENCE_WAV), winsound.SND_FILENAME)


def _play_mp3(path: str) -> bool:
    """用 Windows 自带的 MCI 接口播放 mp3,播完才返回"""
    _play_lead_in_silence()
    mci = ctypes.windll.winmm.mciSendStringW
    if mci(f'open "{path}" type mpegvideo alias ttsclip', None, 0, None) != 0:
        return False
    try:
        mci("play ttsclip wait", None, 0, None)
    finally:
        mci("close ttsclip", None, 0, None)
    return True


def _speak_edge(text: str):
    import asyncio

    path = os.path.join(tempfile.gettempdir(), "tgbot_tts.mp3")
    asyncio.run(edge_tts.Communicate(text, VOICE).save(path))
    if not _play_mp3(path):
        raise RuntimeError("mp3 播放失败")


_sapi = None


def _speak_sapi(text: str):
    global _sapi
    if _sapi is None:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        _sapi = win32com.client.Dispatch("SAPI.SpVoice")
        voices = _sapi.GetVoices()
        for i in range(voices.Count):
            if "Chinese" in voices.Item(i).GetDescription():
                _sapi.Voice = voices.Item(i)
                break
    _play_lead_in_silence()
    _sapi.Speak(text)


def _tts_worker():
    while True:
        text = _queue.get()
        for _ in range(max(1, REPEAT_TIMES)):
            if edge_tts is not None:
                try:
                    _speak_edge(text)
                    continue
                except Exception as e:
                    print(f"[语音] 在线语音失败({e}),回退到系统语音")
            try:
                _speak_sapi(text)
            except Exception as e:
                print(f"[语音] 播报失败: {e}")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._ok('{"status": "TTS server running"}')

    def do_POST(self):
        if self.path != "/speak":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            text = str(json.loads(self.rfile.read(length))["text"])
        except Exception:
            self.send_error(400, "expect JSON body: {\"text\": \"...\"}")
            return
        print(f"[{datetime.now():%H:%M:%S}] {text}")
        _queue.put(text[:500])
        self._ok('{"ok": true}')

    def _ok(self, body: str):
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # 关掉每次请求的默认访问日志
        pass


if __name__ == "__main__":
    threading.Thread(target=_tts_worker, daemon=True).start()
    if edge_tts is None:
        print("提示:未安装 edge-tts,将使用系统自带语音。想要自然音色请执行: pip install edge-tts")
    else:
        print(f"在线语音音色:{VOICE}(联网失败自动回退系统语音)")
    print(f"播报前导静音:{LEAD_IN_MS}ms")
    print(f"每条播报重复次数:{REPEAT_TIMES}")
    print(f"语音服务已启动,监听 0.0.0.0:{PORT},等待 Docker 容器的播报请求...")
    print("保持本窗口运行即可(可最小化)。按 Ctrl+C 退出。")
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        print(f"端口 {PORT} 已被占用:语音服务可能已经在另一个窗口运行了,不需要重复启动。")
        input("按回车键退出...")
        sys.exit(1)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
