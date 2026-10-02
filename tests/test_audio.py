"""Тест перекодирования в MP3 без ffmpeg: python -m unittest discover -s tests"""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from mysuno.audio import convert_to_mp3


class Mp3ConvertTests(unittest.TestCase):
    def test_flac_to_mp3_keeps_duration_and_removes_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "a.flac"
            t = np.linspace(0, 2, 2 * 48000, endpoint=False)
            stereo = np.stack([np.sin(2 * np.pi * 440 * t), np.sin(2 * np.pi * 660 * t)], axis=1) * 0.3
            sf.write(str(src), stereo.astype("float32"), 48000)

            dst = convert_to_mp3(src)

            self.assertEqual(dst.suffix, ".mp3")
            self.assertTrue(dst.exists())
            self.assertFalse(src.exists())
            data, sr = sf.read(str(dst), always_2d=True)
            self.assertEqual((sr, data.shape[1]), (48000, 2))
            self.assertAlmostEqual(len(data) / sr, 2.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
