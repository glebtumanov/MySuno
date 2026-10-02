"""Перекодирование аудио без ffmpeg (libsndfile через soundfile)."""
from __future__ import annotations

from pathlib import Path


def convert_to_mp3(src: Path) -> Path:
    """FLAC/WAV → MP3 рядом с исходником; исходник удаляется."""
    import soundfile as sf

    dst = src.with_suffix(".mp3")
    data, sr = sf.read(str(src), dtype="float32", always_2d=True)
    with sf.SoundFile(str(dst), "w", samplerate=sr, channels=data.shape[1],
                      format="MP3", subtype="MPEG_LAYER_III") as f:
        f.write(data)   # в этой версии soundfile качество не настраивается — libsndfile выбирает VBR сама
    src.unlink(missing_ok=True)
    return dst
