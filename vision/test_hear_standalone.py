#!/usr/bin/env python3
"""P12.3 standalone Hear test — no DSH, no Node, no state machine.

Runs for 15 seconds by default (VHCU_HEAR_TEST_SECONDS overrides), captures the
default speaker WASAPI loopback, segments with webrtcvad, transcribes with the
local faster-whisper base model, and prints the text directly in the terminal.

Usage:
    py -3 vision\test_hear_standalone.py

Before running, play music/video on the machine (system speakers, not muted).
The script prints frame/RMS diagnostics every second so a silent or blocked
capture is obvious. It force-exits after the deadline so a deadlocked soundcard
thread cannot hang the terminal forever.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path

import numpy as np
import soundcard as sc
import webrtcvad
from faster_whisper import WhisperModel

RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = RATE * FRAME_MS // 1000  # 480
SILENCE_SEC = 0.6
MIN_SPEECH_SEC = 0.3
MAX_SEGMENT_SEC = 4.0
PADDING_SEC = 0.2
PADDING_FRAMES = (int(PADDING_SEC * 1000) + FRAME_MS - 1) // FRAME_MS
PREROLL_FRAMES = 10
SECONDS = max(3, int(os.environ.get("VHCU_HEAR_TEST_SECONDS", "15")))

_stop = threading.Event()


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def model_root() -> Path:
    env = os.environ.get("VHCU_MODEL_PATH")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / "models"


def load_model() -> WhisperModel:
    root = model_root()
    local = root / "faster-whisper-base"
    log(f"模型根目录: {root}")
    if (local / "model.bin").is_file():
        log(f"使用本地离线模型: {local} (local_files_only=True)")
        return WhisperModel(str(local), device="cpu", compute_type="int8", local_files_only=True)
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    log(f"本地模型不存在，回退镜像下载到 {root}")
    return WhisperModel("base", device="cpu", compute_type="int8", download_root=str(root))


def pick_loopback():
    log("soundcard: default_speaker ...")
    speaker = sc.default_speaker()
    log(f"default_speaker = {speaker.name!r}")
    candidates = []
    for identifier in (getattr(speaker, "id", None), getattr(speaker, "name", None)):
        if not identifier:
            continue
        try:
            candidates.append(sc.get_microphone(id=str(identifier), include_loopback=True))
            log(f"get_microphone({identifier!r}) ok")
        except Exception as error:  # noqa: BLE001
            log(f"get_microphone({identifier!r}) failed: {error}")
    try:
        mics = sc.all_microphones(include_loopback=True)
        log(f"all_microphones count = {len(mics)}")
        candidates.extend(mics)
    except Exception as error:  # noqa: BLE001
        log(f"all_microphones failed: {error}")
    for mic in candidates:
        if mic is None:
            continue
        if getattr(mic, "isloopback", False):
            log(f"selected loopback = {mic.name!r}")
            return mic
        name = str(getattr(mic, "name", ""))
        if "loopback" in name.lower() or str(getattr(speaker, "name", "")) in name:
            log(f"selected by name = {name!r}")
            return mic
    raise RuntimeError("没有找到 default speaker 的 WASAPI Loopback 设备")


def to_int16(data) -> bytes:
    array = np.asarray(data)
    if array.ndim > 1:
        array = array.mean(axis=1)
    if array.dtype.kind == "f":
        array = np.clip(array, -1.0, 1.0) * 32767.0
    return array.astype(np.int16).tobytes()


def transcribe(model: WhisperModel, frames: list[bytes]) -> int:
    silent = b"\x00\x00" * FRAME_SAMPLES
    padded = [silent] * PADDING_FRAMES + list(frames) + [silent] * PADDING_FRAMES
    audio = np.frombuffer(b"".join(padded), dtype=np.int16).astype(np.float32) / 32768.0
    log(
        f"transcribe: captured={len(frames) * FRAME_MS}ms "
        f"padded={len(padded) * FRAME_MS}ms (+{PADDING_SEC:.1f}s head/tail)"
    )
    started = time.perf_counter()
    segments, info = model.transcribe(
        audio,
        beam_size=1,
        vad_filter=False,
        condition_on_previous_text=False,
    )
    text = "".join(segment.text for segment in segments).strip()
    elapsed = (time.perf_counter() - started) * 1000
    if text:
        log(f"转写 ({len(frames) * FRAME_MS}ms 音频, {elapsed:.0f}ms, lang={info.language}):")
        print(f"TEXT: {text}", flush=True)
        return 1
    log(f"（空转写: {len(frames) * FRAME_MS}ms 音频, {elapsed:.0f}ms）")
    return 0


def capture(model: WhisperModel) -> dict:
    stats = {"frames": 0, "texts": 0, "max_rms": 0.0, "error": None}
    mic = pick_loopback()
    vad = webrtcvad.Vad(2)
    preroll: list[bytes] = []
    speech: list[bytes] = []
    speech_frames = 0
    silence_frames = 0
    deadline = time.monotonic() + SECONDS

    log("opening mic.recorder ...")
    with mic.recorder(samplerate=RATE, channels=1) as recorder:
        log("mic.recorder opened; capture loop started")
        while not _stop.is_set() and time.monotonic() < deadline:
            data = recorder.record(numframes=FRAME_SAMPLES)
            pcm = to_int16(data)
            if len(pcm) < FRAME_SAMPLES * 2:
                continue
            pcm = pcm[: FRAME_SAMPLES * 2]
            stats["frames"] += 1
            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(samples * samples)))
            stats["max_rms"] = max(stats["max_rms"], rms)
            if stats["frames"] % 33 == 0:
                log(
                    f"frames={stats['frames']} rms={rms:.4f} "
                    f"max_rms={stats['max_rms']:.4f} speech_frames={speech_frames}"
                )
            try:
                voiced = vad.is_speech(pcm, RATE)
            except Exception as error:  # noqa: BLE001
                log(f"vad error: {error}")
                voiced = False
            if voiced:
                if not speech:
                    speech.extend(preroll)
                speech.append(pcm)
                speech_frames += 1
                silence_frames = 0
            elif speech:
                speech.append(pcm)
                silence_frames += 1
                if silence_frames * FRAME_MS / 1000.0 >= SILENCE_SEC:
                    if speech_frames * FRAME_MS / 1000.0 >= MIN_SPEECH_SEC:
                        stats["texts"] += transcribe(model, speech)
                    speech = []
                    speech_frames = 0
                    silence_frames = 0
            else:
                preroll.append(pcm)
                if len(preroll) > PREROLL_FRAMES:
                    preroll.pop(0)
            if speech and len(speech) * FRAME_MS / 1000.0 >= MAX_SEGMENT_SEC:
                log(f"segment forced at {MAX_SEGMENT_SEC:.1f}s (voiced={speech_frames})")
                stats["texts"] += transcribe(model, speech)
                speech = []
                speech_frames = 0
                silence_frames = 0

    if speech and speech_frames * FRAME_MS / 1000.0 >= MIN_SPEECH_SEC:
        stats["texts"] += transcribe(model, speech)
    log(
        f"capture ended: frames={stats['frames']} texts={stats['texts']} "
        f"max_rms={stats['max_rms']:.4f}"
    )
    return stats


def main() -> int:
    log("=" * 64)
    log("P12.3 纯净终端 Hear 测试（无 DSH / 无 Node / 无状态机）")
    log(f"时长: {SECONDS}s；请现在播放音乐或视频（系统外放，不要静音）")
    log("=" * 64)
    model = load_model()
    log("whisper model loaded")

    result = {"stats": None, "error": None}

    def worker() -> None:
        try:
            result["stats"] = capture(model)
        except BaseException:  # noqa: BLE001
            result["error"] = traceback.format_exc()
            log("capture failed:")
            print(result["error"], flush=True)

    thread = threading.Thread(target=worker, name="standalone-hear", daemon=True)
    started = time.monotonic()
    thread.start()
    while thread.is_alive() and time.monotonic() - started < SECONDS + 2:
        time.sleep(0.2)
    _stop.set()
    thread.join(timeout=3.0)
    if thread.is_alive():
        log("capture thread did not stop within 3s; forcing process exit")
    stats = result["stats"] or {}
    log("=" * 64)
    log(
        f"summary: frames={stats.get('frames', 0)} texts={stats.get('texts', 0)} "
        f"max_rms={stats.get('max_rms', 0):.4f} error={bool(result['error'])}"
    )
    if not stats.get("frames"):
        log("没有采集到任何帧：soundcard 可能死锁或设备不可用。")
    elif stats.get("max_rms", 0.0) <= 0.0001:
        log("采集到帧但 RMS≈0：系统没有播放声音或默认设备是静音/独占。")
    log("=" * 64)
    print("DONE", flush=True)
    # Force exit: a stuck soundcard native thread must not keep the terminal alive.
    os._exit(0 if not result["error"] else 1)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _stop.set()
        print("interrupted", flush=True)
        os._exit(130)
