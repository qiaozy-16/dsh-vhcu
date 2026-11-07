"""P12.1 Hear: WASAPI loopback capture -> WebRTC VAD -> faster-whisper.

No microphone, no TTS, no conversation: only system playback (loopback) is
captured, segmented on >0.8s silence, transcribed locally with the fixed
``base`` model on CPU (int8), and emitted as ``hear.text`` events.

Dependencies are optional: importing this module never imports soundcard /
webrtcvad / faster_whisper. ``HearService.start()`` does the import and raises
``HearError('E_UNAVAILABLE', ...)`` with a pip hint when something is missing.
"""

from __future__ import annotations

import gc
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from pathlib import Path

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480
SILENCE_SEC = 0.6
MIN_SPEECH_SEC = 0.3
MAX_SEGMENT_SEC = 8.0
OVERLAP_SEC = 0.5
OVERLAP_FRAMES = (int(OVERLAP_SEC * 1000) + FRAME_MS - 1) // FRAME_MS  # 17 frames = 510ms
PADDING_SEC = 0.2
PADDING_FRAMES = (int(PADDING_SEC * 1000) + FRAME_MS - 1) // FRAME_MS  # 7 frames = 210ms
PREROLL_FRAMES = 10  # 0.3s
AMBIENT_RMS_THRESHOLD = 0.03
SHORT_SEGMENT_SEC = 3.0
SHORT_LOW_RMS = 0.05
SHORT_NO_SPEECH = 0.6
_SHORT_PHRASES = {
    "you", "thankyou", "music", "themusic", "the", "hard", "i", "oh", "uh", "um",
    "yeah", "okay", "ok", "a", "an", "and", "is", "it", "this", "that",
    "we", "he", "she", "they", "me", "my", "to", "of", "hover", "applause",
}
ABSOLUTE_BLACKLIST = (
    "i'm not going to be able to do that",
    "i am not going to be able to do that",
    "thank you for watching",
    "thanks for watching",
    "hello",
)
SENSITIVITY = {
    "strict": {
        "no_speech_threshold": 0.5,
        "short_phrase_max_len": 16,
        "short_phrase_duration": 4.0,
        "protect_max_len": 2,
        "long_no_speech_len": 8,
        "high_energy_len": 6,
    },
    "normal": {
        "no_speech_threshold": 0.6,
        "short_phrase_max_len": 12,
        "short_phrase_duration": 3.0,
        "protect_max_len": 5,
        "long_no_speech_len": 10,
        "high_energy_len": 8,
    },
    "sensitive": {
        "no_speech_threshold": 0.8,
        "short_phrase_max_len": 6,
        "short_phrase_duration": 2.0,
        "protect_max_len": 8,
        "long_no_speech_len": 14,
        "high_energy_len": 12,
    },
}
DEFAULT_SENSITIVITY = "normal"
MODEL_NAME = "base"

PIP_HINT = "pip install soundcard webrtcvad-wheels faster-whisper"

# Hugging Face is extremely slow/unreachable from mainland China. Force the
# mirror BEFORE faster_whisper/huggingface_hub is imported; assigning at module
# import time covers the normal daemon path, and force_hf_mirror() re-applies it
# (plus the already-imported huggingface_hub constant) before model load.
HF_MIRROR = "https://hf-mirror.com"
os.environ["HF_ENDPOINT"] = HF_MIRROR


def force_hf_mirror() -> str:
    """Set HF_ENDPOINT and patch an already-imported huggingface_hub constant."""
    os.environ["HF_ENDPOINT"] = HF_MIRROR
    try:
        import huggingface_hub.constants as constants

        constants.ENDPOINT = HF_MIRROR
        if hasattr(constants, "HF_ENDPOINT"):
            constants.HF_ENDPOINT = HF_MIRROR
    except Exception:  # noqa: BLE001
        # huggingface_hub is imported together with faster_whisper; if it is not
        # there yet, the env var above is what its import will read.
        pass
    return HF_MIRROR


class HearError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.hint = hint


class HearAudioTimeout(HearError):
    pass


AUDIO_DEVICE_TIMEOUT = 5.0


def _audio_device_call(label: str, func, timeout: float = AUDIO_DEVICE_TIMEOUT):
    """Run one soundcard device call with a hard 5s wait and explicit logs.

    If the WASAPI/loopback enumeration deadlocks, raise E_AUDIO_DEVICE instead of
    leaving the Hear thread (and the UI) stuck in loading forever.
    """
    print(f"[hear] audio: {label} ...", file=sys.stderr, flush=True)
    box: dict = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["value"] = func()
        except BaseException as error:  # noqa: BLE001
            box["error"] = error
        finally:
            done.set()

    threading.Thread(target=run, name=f"hear-audio-{label}", daemon=True).start()
    if not done.wait(timeout):
        message = f"{label} 超过 {timeout}s 未返回，无法获取系统内录设备"
        print(f"[hear] audio: TIMEOUT {message}", file=sys.stderr, flush=True)
        raise HearAudioTimeout(
            "E_AUDIO_DEVICE",
            message,
            "检查默认扬声器/声卡驱动/独占模式；可重启 Windows 音频服务或换默认播放设备",
        )
    if "error" in box:
        error = box["error"]
        print(f"[hear] audio: ERROR {label}: {error}", file=sys.stderr, flush=True)
        raise HearAudioTimeout(
            "E_AUDIO_DEVICE",
            f"{label} 失败：{error}",
            "soundcard 无法枚举 WASAPI Loopback；检查播放设备与 soundcard 安装",
        )
    print(f"[hear] audio: {label} ok", file=sys.stderr, flush=True)
    return box.get("value")


def default_model_dir() -> Path:
    """Never write model files to the C: user cache by default.

    VHCU_MODEL_PATH wins; otherwise <plugin root>/models (sibling of lib/,
    vision/), e.g. <repo>/models.
    """
    env = os.environ.get("VHCU_MODEL_PATH")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "models"


def local_offline_model_dir(model_root=None) -> Path | None:
    """Return a fully local faster-whisper model dir, or None.

    Accepted layouts (checked in order):
      <root>/faster-whisper-base/model.bin   (manual mirror download)
      <root>/model.bin                       (VHCU_MODEL_PATH points at model dir)
    Every checked path is printed explicitly so a stuck loading can be diagnosed.
    """
    root = (Path(model_root) if model_root else default_model_dir()).expanduser().resolve()
    for candidate in (root / "faster-whisper-base", root):
        found = (candidate / "model.bin").is_file()
        print(
            f"[hear] 检查离线模型路径: {candidate} (model.bin={found})",
            file=sys.stderr,
            flush=True,
        )
        if found:
            return candidate.resolve()
    return None


def cleanup_stale_cache(model_root=None) -> list[str]:
    """Remove stale HF cache/lock artifacts left by failed downloads.

    Only the fixed base cache and lock/incomplete files are touched; the local
    offline model directory itself is never removed.
    """
    root = (Path(model_root) if model_root else default_model_dir()).expanduser().resolve()
    removed: list[str] = []
    names = [".locks"]
    # Only discard the old HF base cache when a complete local model exists;
    # otherwise the cache may be the fallback download source.
    if (root / "faster-whisper-base" / "model.bin").is_file():
        names.append("models--Systran--faster-whisper-base")
    for name in names:
        target = root / name
        if target.is_dir():
            try:
                shutil.rmtree(target)
                removed.append(str(target))
            except Exception as error:  # noqa: BLE001
                print(f"[hear] 清理缓存失败 {target}: {error}", file=sys.stderr, flush=True)
    try:
        for target in root.rglob("*.incomplete"):
            try:
                target.unlink()
                removed.append(str(target))
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    for path in removed:
        print(f"[hear] 已清理旧缓存: {path}", file=sys.stderr, flush=True)
    return removed


def load_whisper_model(whisper_model_cls, model_root):
    """Load a local offline model when present; otherwise use HF mirror download.

    Returns (model, loaded_path, offline). The offline branch passes NO
    download_root and local_files_only=True, so faster-whisper performs no
    network request and does not touch the Hugging Face cache.
    """
    root = (Path(model_root) if model_root else default_model_dir()).expanduser().resolve()
    print(f"[hear] 模型根目录(绝对路径): {root}", file=sys.stderr, flush=True)
    offline = local_offline_model_dir(root)
    if offline is not None:
        local_path = str(offline.resolve())
        print(f"使用本地离线模型加载: {local_path}", file=sys.stderr, flush=True)
        print(
            f"[hear] OFFLINE_MODEL path={local_path} local_files_only=True",
            file=sys.stderr,
            flush=True,
        )
        model = whisper_model_cls(
            local_path,
            device="cpu",
            compute_type="int8",
            local_files_only=True,
        )
        return model, local_path, True
    force_hf_mirror()
    root.mkdir(parents=True, exist_ok=True)
    print(f"[hear] 本地模型不存在，回退镜像下载: {root}", file=sys.stderr, flush=True)
    model = whisper_model_cls(
        MODEL_NAME,
        device="cpu",
        compute_type="int8",
        download_root=str(root),
    )
    return model, str(root), False


def _import_dependencies():
    force_hf_mirror()
    try:
        import numpy as np  # noqa: F401
        import soundcard  # noqa: F401
        import webrtcvad  # noqa: F401
        from faster_whisper import WhisperModel  # noqa: F401
    except Exception as error:  # noqa: BLE001
        raise HearError(
            "E_UNAVAILABLE",
            f"Hear 依赖缺失：{error}",
            PIP_HINT,
        ) from error
    return {"np": np, "sc": soundcard, "webrtcvad": webrtcvad, "WhisperModel": WhisperModel}


def _to_int16(data, np):
    """soundcard returns float32 in [-1, 1]; normalize to mono int16 bytes."""
    array = np.asarray(data)
    if array.ndim > 1:
        array = array.mean(axis=1)
    if array.dtype.kind == "f":
        array = np.clip(array, -1.0, 1.0) * 32767.0
    return array.astype(np.int16).tobytes()


def should_force_cut(frames: list[bytes]) -> bool:
    """P13 pseudo-streaming: cut long continuous speech at MAX_SEGMENT_SEC."""
    return bool(frames) and len(frames) * FRAME_MS / 1000.0 >= MAX_SEGMENT_SEC


_SILENCE_FRAME = b"\x00\x00" * FRAME_SAMPLES


def pad_audio(frames: list[bytes], head: int = PADDING_FRAMES, tail: int = PADDING_FRAMES) -> list[bytes]:
    """P13.1: pad sliced audio with 0.2s of silence at both edges.

    The head keeps the pre-roll context already present in ``frames`` and adds a
    fixed safety margin; the tail prevents whisper from clipping the last phoneme
    at a forced cut. This does not modify the captured stream.
    """
    if not frames:
        return frames
    return [_SILENCE_FRAME] * max(0, head) + list(frames) + [_SILENCE_FRAME] * max(0, tail)


def dedupe_overlap(previous: str, current: str, min_chars: int = 2) -> str:
    """P13.2: strip the longest suffix(previous)==prefix(current) overlap."""
    previous = (previous or "").strip()
    current = (current or "").strip()
    if not previous or not current:
        return current
    for size in range(min(len(previous), len(current)), min_chars - 1, -1):
        if previous[-size:] == current[:size]:
            return current[size:].strip()
    return current


_MEANINGLESS_TOKENS = {
    "music", "applause", "laughter", "hover", "silence", "noise", "sound",
    "speech", "音乐", "掌声", "笑声", "静音", "噪音", "声音",
}
_TAG_RE = re.compile(r"[\[\(（【][^\]\)）】]*[\]\)）】]")


def compute_rms_stats(frames: list[bytes], np) -> tuple[float, float, float, bool]:
    """P16.2/16.3: (average, max, min, has_spike) RMS over 30ms frames."""
    if not frames:
        return (0.0, 0.0, 0.0, False)
    data = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0
    if data.size == 0:
        return (0.0, 0.0, 0.0, False)
    if data.size % FRAME_SAMPLES == 0:
        matrix = data.reshape(-1, FRAME_SAMPLES)
        per_frame = np.sqrt(np.mean(np.square(matrix), axis=1))
        rms = float(np.sqrt(np.mean(np.square(data))))
        rms_max = float(np.max(per_frame))
        rms_min = float(np.min(per_frame))
        has_spike = bool(rms_max > 0.2 and rms_max > max(0.2, rms * 3.0))
        return (rms, rms_max, rms_min, has_spike)
    rms = float(np.sqrt(np.mean(np.square(data))))
    rms_max = float(np.max(np.abs(data)))
    rms_min = float(np.min(np.abs(data)))
    has_spike = bool(rms_max > 0.2 and rms_max > max(0.2, rms * 3.0))
    return (rms, rms_max, rms_min, has_spike)


def compute_rms(frames: list[bytes], np) -> float:
    """P16.1 compatibility helper: normalized average RMS."""
    return compute_rms_stats(frames, np)[0]


def _short_phrase_key(value: str) -> str:
    cleaned = _TAG_RE.sub(" ", value)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", cleaned.casefold(), flags=re.UNICODE)


def classify_transcript(
    text: str,
    duration_sec: float | None = None,
    no_speech_prob: float = 0.0,
    rms: float = 0.0,
    rms_max: float | None = None,
    has_spike: bool = False,
    config: dict | None = None,
) -> dict:
    """P16.6/P16.7: multi-signal hallucination decision.

    The configured sensitivity changes no_speech threshold and length limits;
    ABSOLUTE_BLACKLIST is never affected by sensitivity.

    Order matters:
      1. empty/punctuation/annotation-only -> discard;
      2. absolute blacklist -> discard regardless of length/energy;
      3. repetitive loop -> discard;
      4. high-energy long text (has_spike or rms_max > 0.3, len > 8) -> discard;
      5. high no_speech long text (no_speech > 0.6, len > 10) -> discard;
      6. protected short speech (1-5 chars, no_speech > 0.6, normal RMS,
         no spike) -> KEEP even if it looks like a filler word;
      7. legacy short-phrase rule -> discard only under low confidence/energy.
    Returns {"discard", "reason", "score"} for logging/tests.
    """
    value = str(text or "").strip()
    cfg = config or SENSITIVITY[DEFAULT_SENSITIVITY]
    threshold = float(cfg["no_speech_threshold"])
    if not value:
        return {"discard": True, "reason": "empty", "score": 100}
    stripped = _TAG_RE.sub(" ", value)
    if not re.search(r"[^\W_]", stripped, flags=re.UNICODE):
        return {"discard": True, "reason": "empty", "score": 100}

    plain = " ".join(stripped.casefold().replace("’", "'").split())
    for phrase in ABSOLUTE_BLACKLIST:
        if phrase in plain:
            return {"discard": True, "reason": "blacklist", "score": 100}

    if is_repetitive_text(value):
        return {"discard": True, "reason": "repetitive", "score": 90}

    length = len(value)
    high_energy = bool(has_spike) or (rms_max is not None and float(rms_max) > 0.3)
    if high_energy and length > cfg["high_energy_len"]:
        return {"discard": True, "reason": "high-energy", "score": 80}

    confidence = float(no_speech_prob or 0.0)
    if confidence > threshold and length > cfg["long_no_speech_len"]:
        return {"discard": True, "reason": "long-no-speech", "score": 70}

    no_spike = not has_spike and (rms_max is None or float(rms_max) <= 0.3)
    if (
        confidence > threshold
        and 1 <= length <= cfg["protect_max_len"]
        and SHORT_LOW_RMS <= float(rms or 0.0) <= 0.2
        and no_spike
    ):
        return {"discard": False, "reason": "protected-short", "score": -50}

    key = _short_phrase_key(value)
    if (
        key
        and duration_sec is not None
        and key in _SHORT_PHRASES
        and length <= cfg["short_phrase_max_len"]
        and duration_sec < cfg["short_phrase_duration"]
        and (confidence > threshold or float(rms or 0.0) < SHORT_LOW_RMS)
    ):
        return {"discard": True, "reason": "short-phrase", "score": 40}

    return {"discard": False, "reason": "keep", "score": 0}


def should_discard_transcript(
    text: str,
    duration_sec: float | None = None,
    no_speech_prob: float = 0.0,
    rms: float = 0.0,
    rms_max: float | None = None,
    has_spike: bool = False,
    config: dict | None = None,
) -> bool:
    """Compatibility wrapper around classify_transcript()."""
    return bool(
        classify_transcript(
            text,
            duration_sec=duration_sec,
            no_speech_prob=no_speech_prob,
            rms=rms,
            rms_max=rms_max,
            has_spike=has_spike,
            config=config,
        )["discard"]
    )


def is_repetitive_text(text: str, min_repeat: int = 3) -> bool:
    """P16.4: detect whisper loop hallucinations repeating a word/phrase 3+ times."""
    lowered = str(text or "").casefold()
    words = re.findall(r"[a-z0-9']+", lowered)
    stopwords = {"the", "a", "an", "and", "to", "of", "i", "you", "it", "is", "in", "on"}
    max_n = min(8, max(1, len(words) // min_repeat + 1)) if words else 0
    for size in range(1, max_n + 1):
        counts: dict[tuple[str, ...], int] = {}
        for index in range(0, len(words) - size + 1):
            gram = tuple(words[index:index + size])
            counts[gram] = counts.get(gram, 0) + 1
        for gram, count in counts.items():
            if count < min_repeat:
                continue
            if size > 1 or (len(gram[0]) >= 3 and gram[0] not in stopwords):
                return True
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        max_cn = min(8, max(1, len(run) // min_repeat + 1))
        for size in range(2, max_cn + 1):
            counts = {}
            for index in range(0, len(run) - size + 1):
                gram = run[index:index + size]
                counts[gram] = counts.get(gram, 0) + 1
            if any(count >= min_repeat for count in counts.values()):
                return True
    return False


def _open_loopback(sc):
    """Best-effort default speaker loopback; raises E_AUDIO_DEVICE on failure/timeout."""
    print("[hear] audio: resolving default speaker loopback", file=sys.stderr, flush=True)
    speaker = _audio_device_call("default_speaker", sc.default_speaker)
    print(
        f"[hear] audio: default_speaker = {getattr(speaker, 'name', speaker)!r}",
        file=sys.stderr,
        flush=True,
    )
    candidates = []
    for identifier in (getattr(speaker, "id", None), getattr(speaker, "name", None)):
        if not identifier:
            continue
        try:
            mic = _audio_device_call(
                f"get_microphone({identifier})",
                lambda ident=identifier: sc.get_microphone(id=str(ident), include_loopback=True),
            )
            candidates.append(mic)
        except HearAudioTimeout as error:
            print(f"[hear] audio: get_microphone failed, skip: {error}", file=sys.stderr, flush=True)
            continue
    try:
        mics = _audio_device_call(
            "all_microphones(include_loopback=True)",
            lambda: sc.all_microphones(include_loopback=True),
        )
        candidates.extend(mics or [])
        print(f"[hear] audio: all_microphones count = {len(mics or [])}", file=sys.stderr, flush=True)
    except HearAudioTimeout as error:
        print(f"[hear] audio: all_microphones failed: {error}", file=sys.stderr, flush=True)
    for mic in candidates:
        if mic is None:
            continue
        if getattr(mic, "isloopback", False):
            print(
                f"[hear] audio: selected loopback device = {getattr(mic, 'name', '?')!r}",
                file=sys.stderr,
                flush=True,
            )
            return mic
        name = str(getattr(mic, "name", ""))
        if "loopback" in name.lower() or str(getattr(speaker, "name", "")) in name:
            print(
                f"[hear] audio: selected device by name = {name!r}",
                file=sys.stderr,
                flush=True,
            )
            return mic
    raise HearError(
        "E_AUDIO_DEVICE",
        "没有找到默认扬声器的 WASAPI Loopback 设备",
        "确认系统有正在使用的播放设备；soundcard 仅支持 Windows WASAPI loopback",
    )


class HearService:
    def __init__(self, emit, log=None):
        self._emit_cb = emit
        self._log = log or (lambda message: None)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state = "idle"
        self._model = None
        self._deps = None
        self._last_error = None
        self._model_path = str(default_model_dir())
        self._offline = False
        self._loaded_path = None
        self._last_text = ""
        self._sensitivity = DEFAULT_SENSITIVITY
        self._disable_filter = False
        self.started_at = None
        self.segments = 0

    # ── status / events ────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "state": self._state,
            "running": self._thread is not None and self._thread.is_alive(),
            "model": MODEL_NAME,
            "device": "cpu",
            "computeType": "int8",
            "sampleRate": SAMPLE_RATE,
            "silenceSec": SILENCE_SEC,
            "sensitivity": self._sensitivity,
            "sensitivityConfig": dict(SENSITIVITY[self._sensitivity]),
            "disableFilter": self._disable_filter,
            "modelPath": self._model_path,
            "loadedPath": self._loaded_path,
            "offline": self._offline,
            "hfEndpoint": HF_MIRROR,
            "segments": self.segments,
            "startedAt": self.started_at,
            "lastError": self._last_error,
        }

    def _emit(self, kind: str, **fields) -> None:
        try:
            self._emit_cb(kind, **fields)
        except Exception as error:  # noqa: BLE001
            self._log(f"hear: emit {kind} failed: {error}")

    def configure(self, level: str | None = None, disable_filter: bool | None = None) -> dict:
        """P16.7/P22.2: hot-update sensitivity and raw-transcript filtering."""
        value = str(level).strip().lower() if level is not None else self._sensitivity
        if value not in SENSITIVITY:
            raise HearError("E_PROTO", f"unknown sensitivity level {level!r}")
        self._sensitivity = value
        if disable_filter is not None:
            self._disable_filter = bool(disable_filter)
        self._emit("hear.status", status=self._state, **self.status())
        return {
            "level": value,
            "config": dict(SENSITIVITY[value]),
            "sensitivity": value,
            "disableFilter": self._disable_filter,
        }

    def _set(self, state: str, error: str | None = None) -> None:
        self._state = state
        self._last_error = error
        self._emit("hear.status", status=state, error=error, **self.status())

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self) -> dict:
        # P12.2.4: release any previous Hear thread/model before starting a new one.
        if self._thread is not None and self._thread.is_alive():
            print("[hear] 检测到旧 Hear 线程，先停止并释放模型", file=sys.stderr, flush=True)
            self.stop()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            self._model_path = str(default_model_dir().expanduser().resolve())
            print(f"[hear] 模型根目录(绝对路径): {self._model_path}", file=sys.stderr, flush=True)
            cleanup_stale_cache(self._model_path)
            deps = _import_dependencies()
            self._deps = deps
            self._stop.clear()
            self._last_text = ""
            self.started_at = int(time.time() * 1000)
            self._set("starting")
            self._thread = threading.Thread(target=self._run, name="vhcu-hear", daemon=True)
            self._thread.start()
            return self.status()

    def stop(self) -> dict:
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is None:
            self._state = "idle"
            return self.status()
        self._state = "stopping"
        self._stop.set()
        thread.join(timeout=8.0)
        if thread.is_alive():
            self._last_error = "hear thread did not stop within 8s"
        self._model = None
        self._deps = None
        self._state = "idle"
        self._emit("hear.status", status="idle", error=self._last_error, **self.status())
        gc.collect()
        return self.status()

    # ── pipeline ───────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            self._set("loading")
            self._emit(
                "hear.status",
                status="loading",
                message="检查本地离线模型（models/faster-whisper-base）…",
                **self.status(),
            )
            WhisperModel = self._deps["WhisperModel"]
            model, loaded_path, offline = load_whisper_model(WhisperModel, self._model_path)
            self._offline = offline
            self._loaded_path = loaded_path
            self._emit(
                "hear.status",
                status="loading",
                message=(
                    f"使用本地离线模型加载: {loaded_path}"
                    if offline
                    else f"本地模型不存在，从 HF 镜像 {HF_MIRROR} 下载到 {loaded_path}"
                ),
                **self.status(),
            )
            if self._stop.is_set():
                return
            self._model = model
            self._set("listening")
            self._capture_loop(model)
        except HearError as error:
            self._set("error", f"{error.code}: {error}")
            self._log(f"hear: {error.code}: {error}")
        except Exception as error:  # noqa: BLE001
            self._set("error", f"{type(error).__name__}: {error}")
            self._log(f"hear: pipeline failed: {error}")
        finally:
            if self._state != "error":
                self._state = "idle"

    def _capture_loop(self, model) -> None:
        np = self._deps["np"]
        sc = self._deps["sc"]
        vad = self._deps["webrtcvad"].Vad(2)
        mic = _open_loopback(sc)
        reconnected = False
        while not self._stop.is_set():
            recorder = None
            preroll: list[bytes] = []
            speech: list[bytes] = []
            speech_frames = 0
            silence_frames = 0
            pending_overlap: list[bytes] = []
            pending_at = 0.0
            try:
                mic = _open_loopback(sc)
                print("[hear] audio: opening mic.recorder ...", file=sys.stderr, flush=True)
                recorder = _audio_device_call(
                    "mic.recorder",
                    lambda: mic.recorder(samplerate=SAMPLE_RATE, channels=1),
                )
                print("[hear] audio: mic.recorder opened; capture loop started", file=sys.stderr, flush=True)
                if reconnected:
                    self._set("listening")
                    self._emit(
                        "hear.status",
                        status="listening",
                        message="音频设备已重新连接",
                        **self.status(),
                    )
                    reconnected = False
                with recorder:
                    while not self._stop.is_set():
                        data = recorder.record(numframes=FRAME_SAMPLES)
                        pcm = _to_int16(data, np)
                        if len(pcm) < FRAME_SAMPLES * 2:
                            continue
                        pcm = pcm[: FRAME_SAMPLES * 2]
                        try:
                            voiced = vad.is_speech(pcm, SAMPLE_RATE)
                        except Exception:  # noqa: BLE001
                            voiced = False
                        if voiced:
                            if not speech:
                                # P13.2: start the next segment with the previous tail
                                # (0.5s overlap) when it is still recent; otherwise use
                                # the normal 0.3s pre-roll.
                                if pending_overlap and time.monotonic() - pending_at <= 2.0:
                                    speech.extend(pending_overlap)
                                else:
                                    speech.extend(preroll)
                                pending_overlap = []
                            speech.append(pcm)
                            speech_frames += 1
                            silence_frames = 0
                        elif speech:
                            speech.append(pcm)
                            silence_frames += 1
                            if silence_frames * FRAME_MS / 1000.0 >= SILENCE_SEC:
                                self._flush_segment(model, np, speech, speech_frames)
                                pending_overlap = speech[-OVERLAP_FRAMES:]
                                pending_at = time.monotonic()
                                speech = []
                                speech_frames = 0
                                silence_frames = 0
                        else:
                            preroll.append(pcm)
                            if len(preroll) > PREROLL_FRAMES:
                                preroll.pop(0)
                        # P13: no matter what VAD thinks, cap one utterance at
                        # MAX_SEGMENT_SEC so fast continuous narration keeps producing
                        # subtitle-sized lines.
                        if speech and should_force_cut(speech):
                            print(
                                f"[hear] segment forced at {MAX_SEGMENT_SEC:.1f}s "
                                f"(frames={len(speech)}, voiced={speech_frames})",
                                file=sys.stderr,
                                flush=True,
                            )
                            self._flush_segment(model, np, speech, speech_frames)
                            pending_overlap = speech[-OVERLAP_FRAMES:]
                            pending_at = time.monotonic()
                            speech = []
                            speech_frames = 0
                            silence_frames = 0
                if speech and speech_frames * FRAME_MS / 1000.0 >= MIN_SPEECH_SEC:
                    self._flush_segment(model, np, speech, speech_frames)
            except Exception as error:  # noqa: BLE001
                if self._stop.is_set():
                    break
                print(f"[hear] audio device disconnected: {error}", file=sys.stderr, flush=True)
                self._set("reconnecting", f"audio device disconnected: {error}")
                self._emit(
                    "hear.status",
                    status="reconnecting",
                    message="音频设备断开，尝试重连中",
                    error=str(error),
                    **self.status(),
                )
                reconnected = True
                self._stop.wait(1.0)
            finally:
                if recorder is not None:
                    try:
                        recorder.close()
                    except Exception:  # noqa: BLE001
                        pass

    def _flush_segment(self, model, np, frames: list[bytes], speech_frames: int) -> None:
        if speech_frames * FRAME_MS / 1000.0 < MIN_SPEECH_SEC:
            return
        cfg = SENSITIVITY[self._sensitivity]
        started = time.perf_counter()
        padded = pad_audio(frames)
        rms, rms_max, rms_min, has_spike = compute_rms_stats(frames, np)
        duration_ms = int(len(frames) * FRAME_MS)

        def emit_ambient(reason: str, force: bool = False, force_spike: bool = False) -> None:
            if not force and rms < AMBIENT_RMS_THRESHOLD:
                return
            self._emit(
                "hear.audio",
                ts=int(time.time() * 1000),
                rms=round(rms, 4),
                rms_max=round(rms_max, 4),
                rms_min=round(rms_min, 4),
                has_spike=bool(has_spike or force_spike),
                duration=round(duration_ms / 1000.0, 2),
                durationMs=duration_ms,
                reason=reason,
            )

        audio = np.frombuffer(b"".join(padded), dtype=np.int16).astype(np.float32) / 32768.0
        print(
            f"[hear] transcribe segment: captured={len(frames) * FRAME_MS}ms "
            f"padded={len(padded) * FRAME_MS}ms (+{PADDING_SEC:.1f}s head/tail)",
            file=sys.stderr,
            flush=True,
        )
        try:
            segments, info = model.transcribe(
                audio,
                beam_size=1,
                vad_filter=False,
                condition_on_previous_text=False,
                no_speech_threshold=cfg["no_speech_threshold"],
                log_prob_threshold=-1.0,
            )
            segment_list = list(segments)
            text = "".join(segment.text for segment in segment_list).strip()
            no_speech_prob = max(
                (float(getattr(segment, "no_speech_prob", 0.0) or 0.0) for segment in segment_list),
                default=0.0,
            )
        except Exception as error:  # noqa: BLE001
            self._emit("hear.status", status="transcribe-error", error=str(error), **self.status())
            emit_ambient("transcribe-error")
            return
        decision = (
            {"discard": False, "reason": "raw", "score": 0}
            if self._disable_filter
            else classify_transcript(
                text,
                duration_sec=duration_ms / 1000.0,
                no_speech_prob=no_speech_prob,
                rms=rms,
                rms_max=rms_max,
                has_spike=has_spike,
                config=cfg,
            )
        )
        if decision["discard"]:
            print(
                f"[hear] discarded hallucination: {text[:80]!r} "
                f"(reason={decision['reason']}, score={decision['score']})",
                file=sys.stderr,
                flush=True,
            )
            high_energy = decision["reason"] == "high-energy"
            emit_ambient(decision["reason"], force=high_energy, force_spike=high_energy)
            return
        raw_text = text
        text = dedupe_overlap(self._last_text, text)
        if raw_text != text:
            print(
                f"[hear] dedupe overlap: -{len(raw_text) - len(text)} chars",
                file=sys.stderr,
                flush=True,
            )
        if not text:
            emit_ambient("filtered")
            return
        self._last_text = text
        self.segments += 1
        self._emit(
            "hear.text",
            text=text,
            ts=int(time.time() * 1000),
            durationMs=round((time.perf_counter() - started) * 1000.0, 1),
            audioMs=int(len(frames) * FRAME_MS),
            language=getattr(info, "language", None),
            languageProbability=round(float(getattr(info, "language_probability", 0.0)), 3),
            model=MODEL_NAME,
        )
