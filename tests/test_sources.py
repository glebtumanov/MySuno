"""Тесты исходников для каверов (без GPU): python -m unittest discover -s tests"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

from mysuno import config, sources


def _wav_bytes(seconds: float, sr: int = 22050) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.wav"
        t = np.arange(int(seconds * sr)) / sr
        sf.write(str(p), (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), sr)
        return p.read_bytes()


class SourcesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._patch = mock.patch.object(config, "SOURCES_DIR", self.dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_upload_list_delete(self):
        meta = sources.save_upload(_wav_bytes(12), "Моя песня.wav")
        self.assertEqual(meta["name"], "Моя песня")
        self.assertAlmostEqual(meta["duration"], 12, delta=0.05)
        self.assertEqual([m["id"] for m in sources.list_sources()], [meta["id"]])
        self.assertTrue(sources.source_file(meta["id"]).exists())
        self.assertEqual(sources.get_source(meta["id"])["fmt"], "WAV")
        self.assertEqual(sources.rename_source(meta["id"], "  Новое   имя ")["name"], "Новое имя")
        self.assertEqual(sources.list_sources()[0]["name"], "Новое имя")
        with self.assertRaises(sources.SourceError):
            sources.rename_source(meta["id"], "   ")
        sources.delete_source(meta["id"])
        self.assertEqual(sources.list_sources(), [])

    def test_rejects_short_unknown_and_bad_ids(self):
        with self.assertRaises(sources.SourceError):
            sources.save_upload(_wav_bytes(2), "short.wav")
        with self.assertRaises(sources.UnsupportedAudio):
            sources.save_upload(b"\x00" * 1000, "song.m4a")
        with self.assertRaises(sources.UnsupportedAudio):
            sources.save_upload(b"garbage" * 1000, "song.mp3")
        with self.assertRaises(KeyError):
            sources.get_source("../../library")
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_cut_segment(self):
        meta = sources.save_upload(_wav_bytes(30), "a.wav")
        src = sources.source_file(meta["id"])
        out = self.dir / "job"
        whole, length = sources.cut_segment(src, 0, None, out)
        self.assertEqual(whole, src)
        self.assertAlmostEqual(length, 30, delta=0.05)
        part, length = sources.cut_segment(src, 5, 20, out)
        self.assertNotEqual(part, src)
        self.assertAlmostEqual(length, 15, delta=0.01)
        self.assertAlmostEqual(sf.info(str(part)).duration, 15, delta=0.01)
        _, length = sources.cut_segment(src, 25, 999, out)   # конец обрезается по длине файла
        self.assertAlmostEqual(length, 5, delta=0.01)
        with self.assertRaises(sources.SourceError):
            sources.cut_segment(src, 10, 12, out)


if __name__ == "__main__":
    unittest.main()
