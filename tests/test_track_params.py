"""Сохранение параметров генерации с треком (без GPU): python -m unittest discover -s tests -t ."""
import json
import unittest
from pathlib import Path

import numpy as np

from mysuno import jobs
from mysuno.engine import _ACE_DROP, _jsonable


class JsonableTest(unittest.TestCase):
    def test_drops_heavy_and_non_json(self):
        params = {"seed": np.int64(42), "shift": np.float32(3.0), "audio_codes": "<|audio_code_1|>" * 1000,
                  "src_audio": "C:/tmp/src.wav", "tensor": object(), "timesteps": [np.float64(0.5), 0.25],
                  "path": Path("x.wav"), "nested": {"bpm": 120, "obj": object()}}
        out = _jsonable(params, drop=_ACE_DROP)
        self.assertEqual(out, {"seed": 42, "shift": 3.0, "timesteps": [0.5, 0.25], "path": "x.wav",
                               "nested": {"bpm": 120}})
        json.dumps(out)


class CoverInfoTest(unittest.TestCase):
    def test_not_cover(self):
        self.assertIsNone(jobs._cover_info({"task": None}, {}, {}))

    def test_cover_segment_and_method(self):
        req = {"task": "cover", "source_id": "s1", "source_name": "Песня", "start": 12.5, "duration": 30,
               "cover_strength": 0.6, "cover_noise": 0.2, "end": None}
        result = {"cover": {"mode": "auto", "methods": ["cover", "edit"], "flow_n_max": 0.92}}
        info = jobs._cover_info(req, result, {"method": "edit"})
        self.assertEqual(info["method"], "edit")
        self.assertEqual((info["start"], info["end"], info["length"]), (12.5, 42.5, 30.0))
        self.assertEqual(info["methods"], ["cover", "edit"])
        self.assertEqual((info["strength"], info["noise"], info["source_id"]), (0.6, 0.2, "s1"))


if __name__ == "__main__":
    unittest.main()
