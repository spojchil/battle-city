"""基于小米 MiMo TTS 的测试语音合成。

输出 16kHz、16bit、单声道 PCM，与灵矽设备协议的上行音频格式一致。
合成结果按（音色、风格、文本）缓存到本地，避免重复调用。
"""

import base64
import hashlib
import io
import os
import wave
from pathlib import Path

import httpx
import numpy as np

MIMO_URL = "https://api.xiaomimimo.com/v1/chat/completions"
TARGET_RATE = 16000

# mimo-v2.5-tts 预置中文音色
VOICES = ("冰糖", "茉莉", "苏打", "白桦")

CACHE_DIR = Path(os.getenv("TTS_CACHE_DIR", Path.home() / ".cache" / "linx-game" / "tts"))


def _resample(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return pcm
    duration = len(pcm) / src_rate
    src_t = np.linspace(0, duration, num=len(pcm), endpoint=False)
    dst_t = np.linspace(0, duration, num=int(duration * dst_rate), endpoint=False)
    return np.interp(dst_t, src_t, pcm).astype(np.int16)


def synthesize(text: str, voice: str = "冰糖", style: str | None = None) -> bytes:
    """合成语音，返回 16kHz PCM16LE 单声道字节。"""
    key = hashlib.sha256(f"{voice}\0{style or ''}\0{text}".encode()).hexdigest()[:32]
    cached = CACHE_DIR / f"{key}.pcm"
    if cached.exists():
        return cached.read_bytes()

    messages = []
    if style:
        messages.append({"role": "user", "content": style})
    messages.append({"role": "assistant", "content": text})
    resp = httpx.post(
        MIMO_URL,
        headers={"Authorization": f"Bearer {os.environ['MIMO_API_KEY']}"},
        json={"model": "mimo-v2.5-tts", "messages": messages, "audio": {"format": "wav", "voice": voice}},
        timeout=60,
    )
    resp.raise_for_status()
    wav_bytes = base64.b64decode(resp.json()["choices"][0]["message"]["audio"]["data"])

    with wave.open(io.BytesIO(wav_bytes)) as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    out = _resample(pcm, rate, TARGET_RATE).tobytes()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(out)
    return out
