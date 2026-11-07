"""P12.1 Hear unit tests: no audio hardware and no whisper model required."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import numpy as np

from vhcu_daemon.hear import (
    FRAME_SAMPLES,
    MAX_SEGMENT_SEC,
    OVERLAP_FRAMES,
    PADDING_FRAMES,
    PADDING_SEC,
    SILENCE_SEC,
    HearAudioTimeout,
    HearError,
    HearService,
    _audio_device_call,
    _open_loopback,
    _to_int16,
    cleanup_stale_cache,
    SENSITIVITY,
    classify_transcript,
    compute_rms,
    compute_rms_stats,
    dedupe_overlap,
    is_repetitive_text,
    default_model_dir,
    load_whisper_model,
    local_offline_model_dir,
    pad_audio,
    should_discard_transcript,
    should_force_cut,
)


class HearConfigTest(unittest.TestCase):
    def test_model_dir_honours_env_and_avoids_user_cache(self):
        with mock.patch.dict(os.environ, {"VHCU_MODEL_PATH": "D:/vhcu-models"}, clear=False):
            self.assertEqual(default_model_dir(), Path("D:/vhcu-models").resolve())
        with mock.patch.dict(os.environ, {}, clear=True):
            fallback = default_model_dir()
        self.assertEqual(fallback.name, "models")
        self.assertNotIn(".cache", str(fallback).lower())
        self.assertTrue(str(fallback).endswith(str(Path("dsh-vhcu") / "models")))

    def test_int16_conversion_downmixes_and_clips(self):
        data = np.array([[1.0, -1.0], [0.0, 0.0]], dtype=np.float32)
        pcm = _to_int16(data, np)
        values = np.frombuffer(pcm, dtype=np.int16)
        self.assertEqual(len(values), 2)
        self.assertEqual(int(values[0]), 0)
        self.assertEqual(int(values[1]), 0)


class HearServiceTest(unittest.TestCase):
    def test_missing_dependencies_raise_e_unavailable(self):
        service = HearService(emit=lambda *args, **kwargs: None)
        with mock.patch(
            "vhcu_daemon.hear._import_dependencies",
            side_effect=HearError("E_UNAVAILABLE", "deps missing", "pip install soundcard"),
        ):
            with self.assertRaises(HearError) as ctx:
                service.start()
        self.assertEqual(ctx.exception.code, "E_UNAVAILABLE")
        self.assertIn("pip install", ctx.exception.hint)

    def test_initial_status_and_idempotent_stop(self):
        events = []
        service = HearService(emit=lambda kind, **fields: events.append((kind, fields)))
        status = service.status()
        self.assertEqual(status["state"], "idle")
        self.assertFalse(status["running"])
        self.assertEqual(status["model"], "base")
        self.assertEqual(status["device"], "cpu")
        self.assertEqual(status["computeType"], "int8")
        stopped = service.stop()
        self.assertEqual(stopped["state"], "idle")

    def test_hf_endpoint_is_forced_to_mirror(self):
        from vhcu_daemon.hear import HF_MIRROR, force_hf_mirror

        with mock.patch.dict(os.environ, {"HF_ENDPOINT": "https://huggingface.co"}, clear=False):
            self.assertEqual(force_hf_mirror(), HF_MIRROR)
            self.assertEqual(os.environ["HF_ENDPOINT"], "https://hf-mirror.com")
        status = HearService(emit=lambda *args, **kwargs: None).status()
        self.assertEqual(status["hfEndpoint"], "https://hf-mirror.com")


class OfflineModelTest(unittest.TestCase):
    class FakeWhisper:
        calls = []

        def __init__(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    def tearDown(self):
        self.FakeWhisper.calls = []

    def test_local_model_is_loaded_without_download_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp) / "faster-whisper-base"
            model_dir.mkdir()
            (model_dir / "model.bin").write_bytes(b"fake")
            found = local_offline_model_dir(tmp)
            self.assertEqual(found, model_dir)

            model, loaded_path, offline = load_whisper_model(self.FakeWhisper, tmp)
            self.assertTrue(offline)
            self.assertEqual(loaded_path, str(model_dir))
            args, kwargs = self.FakeWhisper.calls[0]
            self.assertEqual(args[0], str(model_dir))
            self.assertEqual(kwargs["device"], "cpu")
            self.assertEqual(kwargs["compute_type"], "int8")
            self.assertTrue(kwargs["local_files_only"])
            self.assertNotIn("download_root", kwargs)
            self.assertIsInstance(model, self.FakeWhisper)

    def test_stale_cache_is_removed_but_local_model_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp) / "faster-whisper-base"
            model_dir.mkdir()
            (model_dir / "model.bin").write_bytes(b"fake")
            locks = Path(tmp) / ".locks"
            locks.mkdir()
            cache = Path(tmp) / "models--Systran--faster-whisper-base"
            cache.mkdir()
            (cache / "blob.incomplete").write_bytes(b"partial")

            removed = cleanup_stale_cache(tmp)
            self.assertFalse(locks.exists())
            self.assertFalse(cache.exists())
            self.assertTrue((model_dir / "model.bin").is_file())
            self.assertTrue(any(".locks" in path for path in removed))

    def test_missing_local_model_falls_back_to_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(local_offline_model_dir(tmp))
            _, loaded_path, offline = load_whisper_model(self.FakeWhisper, tmp)
            self.assertFalse(offline)
            self.assertEqual(loaded_path, tmp)
            args, kwargs = self.FakeWhisper.calls[0]
            self.assertEqual(args[0], "base")
            self.assertEqual(kwargs["download_root"], tmp)
            self.assertEqual(os.environ["HF_ENDPOINT"], "https://hf-mirror.com")


class AudioDeviceTest(unittest.TestCase):
    class Speaker:
        id = "speaker-id"
        name = "Speakers"

    class Mic:
        name = "Speakers (loopback)"
        isloopback = True

    def test_audio_call_returns_and_times_out(self):
        self.assertEqual(_audio_device_call("inline", lambda: 42, timeout=1.0), 42)
        with self.assertRaises(HearAudioTimeout) as ctx:
            _audio_device_call("slow", lambda: time.sleep(0.3), timeout=0.05)
        self.assertEqual(ctx.exception.code, "E_AUDIO_DEVICE")
        self.assertIn("超过", str(ctx.exception))

    def test_open_loopback_selects_default_speaker_device(self):
        mic = self.Mic()

        class FakeSoundcard:
            def default_speaker(self):
                return AudioDeviceTest.Speaker()

            def get_microphone(self, id=None, include_loopback=False):
                return mic

            def all_microphones(self, include_loopback=False):
                return [mic]

        self.assertIs(_open_loopback(FakeSoundcard()), mic)

    def test_open_loopback_error_is_audio_device(self):
        class Broken:
            def default_speaker(self):
                raise RuntimeError("no speakers")

        with self.assertRaises(HearError) as ctx:
            _open_loopback(Broken())
        self.assertEqual(ctx.exception.code, "E_AUDIO_DEVICE")
        self.assertIn("no speakers", str(ctx.exception))


class PseudoStreamingTest(unittest.TestCase):
    def test_tuning_constants(self):
        self.assertEqual(SILENCE_SEC, 0.6)
        self.assertEqual(MAX_SEGMENT_SEC, 8.0)
        self.assertEqual(PADDING_SEC, 0.2)
        self.assertEqual(PADDING_FRAMES, 7)  # 210ms, at least 0.2s
        self.assertEqual(OVERLAP_FRAMES, 17)  # 510ms, at least 0.5s

    def test_pad_audio_adds_head_and_tail_silence(self):
        frame = b"\x01\x00"
        padded = pad_audio([frame] * 5)
        self.assertEqual(len(padded), 5 + PADDING_FRAMES * 2)
        silence = b"\x00\x00" * 480
        self.assertEqual(padded[0], silence)
        self.assertEqual(padded[-1], silence)
        self.assertEqual(padded[PADDING_FRAMES], frame)
        self.assertEqual(len(pad_audio([])), 0)

    def test_force_cut_at_eight_seconds(self):
        frame = b"\x00\x00"
        self.assertFalse(should_force_cut([]))
        self.assertFalse(should_force_cut([frame] * 266))  # 7.98s
        self.assertTrue(should_force_cut([frame] * 267))   # 8.01s

    def test_dedupe_overlap(self):
        self.assertEqual(dedupe_overlap("促使美国杜鲁门", "杜鲁门政府宣布"), "政府宣布")
        self.assertEqual(dedupe_overlap("第一段结束", "第二段开始"), "第二段开始")
        self.assertEqual(dedupe_overlap("", "全新文本"), "全新文本")
        self.assertEqual(dedupe_overlap("一", "一二"), "一二")  # overlap below min_chars


class DedupeIntegrationTest(unittest.TestCase):
    def test_flush_segment_removes_overlap_and_emits_raw_text(self):
        from types import SimpleNamespace

        events = []

        class FakeModel:
            def transcribe(self, audio, **kwargs):
                return [SimpleNamespace(text="杜鲁门政府宣布")], SimpleNamespace(
                    language="zh", language_probability=0.99
                )

        service = HearService(emit=lambda kind, **fields: events.append((kind, fields)))
        service._last_text = "促使美国杜鲁门"
        frame = b"\x00\x00" * 480
        service._flush_segment(FakeModel(), np, [frame] * 30, 30)
        texts = [fields["text"] for kind, fields in events if kind == "hear.text"]
        self.assertEqual(texts, ["政府宣布"])
        self.assertEqual(service._last_text, "政府宣布")


class HallucinationFilterTest(unittest.TestCase):
    def test_discards_noise_tokens_and_punctuation(self):
        for text in ["", "   ", "...", "!!!", "♪ ♫", "[Music]", "[Applause]",
                     "[Hover]", "[Music] [Applause]"]:
            self.assertTrue(should_discard_transcript(text), repr(text))
        for text in ["Hover", "Music", "The Music"]:
            self.assertTrue(should_discard_transcript(text, 1.0, 0.9, 0.01), text)

    def test_keeps_real_speech_and_other_scripts(self):
        for text in ["请打开设置", "打开音乐播放器", "Hover 请点击这里",
                     "안녕하세요", "Nice to meet you", "Open the settings"]:
            self.assertFalse(should_discard_transcript(text), repr(text))

    def test_absolute_blacklist_wins(self):
        for text in ["Hello", "Hello world", "Thank you for watching",
                     "I'm not going to be able to do that"]:
            self.assertTrue(should_discard_transcript(text, 1.0, 0.1, 0.2), text)

    def test_transcribe_passes_thresholds_and_discards_music(self):
        events = []
        captured = {}

        class FakeModel:
            def transcribe(self, audio, **kwargs):
                captured.update(kwargs)
                return [SimpleNamespace(text="[Music]")], SimpleNamespace(
                    language="en", language_probability=0.5
                )

        service = HearService(emit=lambda kind, **fields: events.append((kind, fields)))
        frame = b"\x00\x00" * 480
        service._flush_segment(FakeModel(), np, [frame] * 30, 30)
        self.assertEqual(captured["no_speech_threshold"], 0.6)
        self.assertEqual(captured["log_prob_threshold"], -1.0)
        self.assertEqual(events, [])


class AmbientAudioTest(unittest.TestCase):
    def test_compute_rms_silence_and_signal(self):
        silence = b"\x00\x00" * 480
        loud = (12000).to_bytes(2, "little", signed=True) * 480
        self.assertEqual(compute_rms([], np), 0.0)
        self.assertEqual(compute_rms([silence] * 5, np), 0.0)
        self.assertGreater(compute_rms([loud] * 5, np), 0.1)

    def test_filtered_music_emits_ambient_when_loud(self):
        events = []

        class EmptyModel:
            def transcribe(self, audio, **kwargs):
                return [], SimpleNamespace(language="en", language_probability=0.1)

        service = HearService(emit=lambda kind, **fields: events.append((kind, fields)))
        loud = (12000).to_bytes(2, "little", signed=True) * 480
        service._flush_segment(EmptyModel(), np, [loud] * 30, 30)
        audio = [fields for kind, fields in events if kind == "hear.audio"]
        self.assertEqual(len(audio), 1)
        self.assertGreater(audio[0]["rms"], 0.01)
        self.assertEqual(audio[0]["durationMs"], 900)
        self.assertEqual(audio[0]["reason"], "empty")

    def test_silence_does_not_emit_ambient(self):
        events = []

        class EmptyModel:
            def transcribe(self, audio, **kwargs):
                return [], SimpleNamespace(language="en", language_probability=0.1)

        service = HearService(emit=lambda kind, **fields: events.append((kind, fields)))
        frame = b"\x00\x00" * 480
        service._flush_segment(EmptyModel(), np, [frame] * 30, 30)
        self.assertEqual(events, [])


class ShortFragmentAndRmsStatsTest(unittest.TestCase):
    def test_short_phrases_discard_only_on_low_energy(self):
        for text in ["You", "The", "The Music", "Music", "I", "Oh"]:
            self.assertTrue(should_discard_transcript(text, 1.5, 0.9, 0.01), text)
        for text in ["I think so", "You know what", "请打开设置", "你好世界"]:
            self.assertFalse(should_discard_transcript(text, 1.5, 0.1, 0.01), text)
        self.assertFalse(should_discard_transcript("You", 3.0, 0.9, 0.01))

    def test_protected_short_speech_overrides_short_phrase(self):
        # 1-5 chars, high no_speech, normal RMS, no spike -> keep.
        self.assertFalse(should_discard_transcript("You", 1.5, 0.9, 0.1, 0.15, False))
        self.assertFalse(should_discard_transcript("你好", 1.5, 0.9, 0.05, 0.1, False))
        # Low energy still discards.
        self.assertTrue(should_discard_transcript("You", 1.5, 0.9, 0.01, 0.1, False))
        # Long text is not protected.
        self.assertTrue(
            should_discard_transcript("this is a hallucinated sentence", 1.5, 0.9, 0.1, 0.15, False)
        )
        # High-energy long text is discarded even with low no_speech.
        self.assertTrue(
            should_discard_transcript("this is an explosion hallucination", 1.5, 0.1, 0.1, 0.5, True)
        )
        # 3.5s short phrase is beyond the short-duration rule.
        self.assertFalse(should_discard_transcript("You", 3.5, 0.9, 0.01))

    def test_rms_stats_expose_max_min_and_spike(self):
        silence = b"\x00\x00" * 480
        loud = (12000).to_bytes(2, "little", signed=True) * 480
        rms, rms_max, rms_min, has_spike = compute_rms_stats([silence] * 20 + [loud], np)
        self.assertGreater(rms, 0.05)
        self.assertGreater(rms_max, 0.3)
        self.assertEqual(rms_min, 0.0)
        self.assertTrue(has_spike)
        self.assertEqual(compute_rms_stats([], np), (0.0, 0.0, 0.0, False))

    def test_ambient_event_carries_rms_max_min(self):
        events = []

        class EmptyModel:
            def transcribe(self, audio, **kwargs):
                return [], SimpleNamespace(language="en", language_probability=0.1)

        service = HearService(emit=lambda kind, **fields: events.append((kind, fields)))
        loud = (12000).to_bytes(2, "little", signed=True) * 480
        service._flush_segment(EmptyModel(), np, [loud] * 30, 30)
        audio = [fields for kind, fields in events if kind == "hear.audio"][0]
        self.assertIn("rms_max", audio)
        self.assertIn("rms_min", audio)
        self.assertGreaterEqual(audio["rms_max"], audio["rms"])


class RepetitionHallucinationTest(unittest.TestCase):
    def test_repeated_phrases_are_discarded(self):
        text = "When the fire burns out " * 4
        self.assertTrue(is_repetitive_text(text))
        self.assertTrue(is_repetitive_text("fire fire fire"))
        self.assertTrue(is_repetitive_text("爆炸爆炸爆炸"))
        self.assertFalse(is_repetitive_text("请打开设置，然后点击保存"))
        self.assertFalse(is_repetitive_text("When the fire burns out"))
        self.assertFalse(is_repetitive_text("the the the"))

    def test_flush_discards_repetitive_text_before_emit(self):
        events = []

        class RepeatModel:
            def transcribe(self, audio, **kwargs):
                return [SimpleNamespace(text="When the fire burns out " * 4, no_speech_prob=0.2)], SimpleNamespace(
                    language="en", language_probability=0.9
                )

        service = HearService(emit=lambda kind, **fields: events.append((kind, fields)))
        loud = (12000).to_bytes(2, "little", signed=True) * 480
        service._flush_segment(RepeatModel(), np, [loud] * 30, 30)
        self.assertEqual([kind for kind, _ in events if kind == "hear.text"], [])
        self.assertTrue(any(kind == "hear.audio" for kind, _ in events))


class MultiSignalClassifierTest(unittest.TestCase):
    def test_reasons_for_each_rule(self):
        self.assertEqual(classify_transcript("Hello")["reason"], "blacklist")
        self.assertEqual(classify_transcript("Thanks for watching")["reason"], "blacklist")
        self.assertEqual(classify_transcript("When the fire burns out " * 4)["reason"], "repetitive")
        high = classify_transcript("explosion hallucination", no_speech_prob=0.1, rms=0.1,
                                   rms_max=0.5, has_spike=True)
        self.assertEqual(high["reason"], "high-energy")
        self.assertTrue(high["discard"])
        long_no_speech = classify_transcript("this is a long hallucinated sentence",
                                             no_speech_prob=0.9, rms=0.1, rms_max=0.15)
        self.assertEqual(long_no_speech["reason"], "long-no-speech")
        protected = classify_transcript("You", no_speech_prob=0.9, rms=0.1, rms_max=0.15)
        self.assertEqual(protected["reason"], "protected-short")
        self.assertFalse(protected["discard"])

    def test_high_energy_long_emits_loud_audio_only(self):
        events = []

        class LoudModel:
            def transcribe(self, audio, **kwargs):
                return [SimpleNamespace(text="this is an explosion hallucination", no_speech_prob=0.2)], SimpleNamespace(
                    language="en", language_probability=0.9
                )

        service = HearService(emit=lambda kind, **fields: events.append((kind, fields)))
        silence = b"\x00\x00" * 480
        loud = (12000).to_bytes(2, "little", signed=True) * 480
        service._flush_segment(LoudModel(), np, [silence] * 20 + [loud], 21)
        self.assertEqual([kind for kind, _ in events if kind == "hear.text"], [])
        audio = [fields for kind, fields in events if kind == "hear.audio"]
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0]["reason"], "high-energy")
        self.assertTrue(audio[0]["has_spike"])


class SensitivityConfigTest(unittest.TestCase):
    def test_configure_updates_service_and_status(self):
        service = HearService(emit=lambda *args, **kwargs: None)
        self.assertEqual(service.status()["sensitivity"], "normal")
        result = service.configure("strict")
        self.assertEqual(result["level"], "strict")
        self.assertEqual(service.status()["sensitivity"], "strict")
        self.assertEqual(service.status()["sensitivityConfig"]["no_speech_threshold"], 0.5)
        service.configure("sensitive")
        self.assertEqual(service.status()["sensitivityConfig"]["no_speech_threshold"], 0.8)
        with self.assertRaises(HearError):
            service.configure("bogus")

    def test_classifier_sensitivity_ordering(self):
        strict = SENSITIVITY["strict"]
        normal = SENSITIVITY["normal"]
        sensitive = SENSITIVITY["sensitive"]
        # 3-char protected in normal/sensitive, not in strict.
        self.assertFalse(classify_transcript("You", 1.5, 0.7, 0.1, 0.15, False, normal)["discard"])
        self.assertTrue(classify_transcript("You", 1.5, 0.7, 0.1, 0.15, False, strict)["discard"])
        self.assertFalse(classify_transcript("You", 1.5, 0.7, 0.1, 0.15, False, sensitive)["discard"])
        # 9-char long text: strict discards, sensitive keeps.
        text = "some words"
        self.assertTrue(classify_transcript(text, 2.0, 0.7, 0.1, 0.15, False, strict)["discard"])
        self.assertFalse(classify_transcript(text, 2.0, 0.7, 0.1, 0.15, False, sensitive)["discard"])
        # Absolute blacklist ignores sensitivity.
        for cfg in (strict, normal, sensitive):
            self.assertTrue(classify_transcript("Hello", 1.0, 0.1, 0.1, 0.1, False, cfg)["discard"])

    def test_whisper_threshold_follows_sensitivity(self):
        captured = []

        class FakeModel:
            def transcribe(self, audio, **kwargs):
                captured.append(kwargs["no_speech_threshold"])
                return [], SimpleNamespace(language="en", language_probability=0.1)

        service = HearService(emit=lambda *args, **kwargs: None)
        service.configure("strict")
        frame = b"\x00\x00" * 480
        service._flush_segment(FakeModel(), np, [frame] * 30, 30)
        service.configure("sensitive")
        service._flush_segment(FakeModel(), np, [frame] * 30, 30)
        self.assertEqual(captured, [0.5, 0.8])


class HearRawFilterTest(unittest.TestCase):
    def test_disable_filter_hot_update_is_reflected_in_status(self):
        service = HearService(lambda *args, **kwargs: None)
        result = service.configure(disable_filter=True)
        self.assertTrue(result["disableFilter"])
        self.assertTrue(service.status()["disableFilter"])
        result = service.configure(disable_filter=False)
        self.assertFalse(result["disableFilter"])


class HearReconnectTest(unittest.TestCase):
    def test_audio_device_error_retries_and_reopens(self):
        events = []
        service = HearService(lambda kind, **fields: events.append((kind, fields)))
        class Vad:
            def is_speech(self, pcm, rate):
                return False
        service._deps = {
            "np": np,
            "sc": object(),
            "webrtcvad": SimpleNamespace(Vad=lambda mode: Vad()),
        }

        class BadRecorder:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def record(self, numframes):
                raise OSError("device unplugged")
            def close(self):
                pass

        class GoodRecorder:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def record(self, numframes):
                service._stop.set()
                return b"\x00" * (FRAME_SAMPLES * 2)
            def close(self):
                pass

        recorders = [BadRecorder(), GoodRecorder()]
        mic = SimpleNamespace(recorder=lambda **kwargs: object())
        with mock.patch("vhcu_daemon.hear._open_loopback", return_value=mic), mock.patch(
            "vhcu_daemon.hear._audio_device_call",
            side_effect=lambda name, fn: recorders.pop(0),
        ), mock.patch.object(service._stop, "wait", return_value=True):
            service._capture_loop(object())

        statuses = [fields.get("status") for kind, fields in events if kind == "hear.status"]
        self.assertIn("reconnecting", statuses)
        self.assertIn("listening", statuses)


class RawTranscriptSwitchTest(unittest.TestCase):
    def test_disabled_filter_emits_blacklisted_text_for_debugging(self):
        from types import SimpleNamespace

        events = []

        class FakeModel:
            def transcribe(self, audio, **kwargs):
                return [SimpleNamespace(text="Hello")], SimpleNamespace(
                    language="en", language_probability=0.99
                )

        service = HearService(emit=lambda kind, **fields: events.append((kind, fields)))
        service._disable_filter = True
        frame = b"\x00\x00" * 480
        service._flush_segment(FakeModel(), np, [frame] * 30, 30)
        texts = [fields["text"] for kind, fields in events if kind == "hear.text"]
        self.assertEqual(texts, ["Hello"])
        self.assertTrue(service.status()["disableFilter"])
