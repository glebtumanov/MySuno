"""Подготовка текста песни для ACE-Step: нормализация структурных тегов и проверка метрики строк.

Зачем: модель обучена на английских тегах структуры ([Verse], [Chorus]…). Русские «[Припев]» она может спеть как слова,
а текст без тегов получает худшую структуру. Документация ACE: 6–10 слогов в строке, пустая строка между секциями.
"""
from __future__ import annotations

import re

# русское название секции → английский тег ACE
_SECTION_MAP = {
    "вступление": "Intro", "интро": "Intro", "intro": "Intro",
    "куплет": "Verse", "verse": "Verse",
    "предприпев": "Pre-Chorus", "pre-chorus": "Pre-Chorus", "prechorus": "Pre-Chorus",
    "припев": "Chorus", "chorus": "Chorus", "хук": "Chorus",
    "бридж": "Bridge", "мост": "Bridge", "bridge": "Bridge",
    "проигрыш": "Instrumental", "инструментал": "Instrumental", "instrumental": "Instrumental",
    "соло": "Solo", "solo": "Solo",
    "аутро": "Outro", "окончание": "Outro", "концовка": "Outro", "outro": "Outro",
    "финальный припев": "Final Chorus", "final chorus": "Final Chorus",
}
_TAG = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*$")
_VOWELS = set("аеёиоуыэюяaeiouy")


def count_syllables(line: str) -> int:
    """Грубая оценка числа слогов (русский: слогов ≈ гласных)."""
    return sum(1 for ch in line.lower() if ch in _VOWELS)


def _canon_tag(inner: str) -> str:
    """«Припев - громкий» → «Chorus - громкий» → оставляем описание после дефиса как есть; название приводим к английскому."""
    head, sep, tail = inner.partition("-")
    head = head.strip()
    m = re.match(r"^(.*?)(\s+\d+)?$", head)
    name, num = (m.group(1).strip(), (m.group(2) or "").strip()) if m else (head, "")
    eng = _SECTION_MAP.get(name.lower())
    if eng is None:
        return inner.strip()          # неизвестный тег (например [raspy vocal]) — не трогаем
    out = eng + (f" {num}" if num else "")
    return f"{out} - {tail.strip()}" if sep and tail.strip() else out


def normalize(text: str, auto_structure: bool = True) -> tuple[str, list[str]]:
    """Возвращает (текст для модели, предупреждения). Не меняет слова песни."""
    warnings: list[str] = []
    lines = [ln.rstrip() for ln in text.replace("﻿", "").replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    for ln in lines:
        m = _TAG.match(ln)
        out.append(f"[{_canon_tag(m.group(1))}]" if m else ln)
    text = "\n".join(out).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    # пустая строка перед каждым тегом (кроме первого)
    text = re.sub(r"(?<!\n)\n(\[[^\]]+\])", r"\n\n\1", text)

    has_tags = any(_TAG.match(ln) for ln in text.split("\n"))
    if not has_tags and auto_structure:
        blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
        if len(blocks) >= 2:
            seen: dict[str, int] = {}
            verse = 0
            tagged = []
            for b in blocks:
                key = re.sub(r"\W+", " ", b.lower()).strip()
                if key in seen:
                    tag = "Chorus"
                else:
                    seen[key] = 1
                    verse += 1
                    tag = f"Verse {verse}"
                tagged.append(f"[{tag}]\n{b.strip()}")
            text = "\n\n".join(tagged)
            warnings.append("В тексте не было тегов структуры — расставил автоматически ([Verse]/[Chorus])")
        elif blocks:
            warnings.append("Текст без секций: разделите куплеты и припев пустыми строками и подпишите [Куплет]/[Припев]")

    for i, ln in enumerate(text.split("\n"), 1):
        if not ln.strip() or _TAG.match(ln):
            continue
        n = count_syllables(ln)
        if n > 14:
            warnings.append(f"Строка слишком длинная ({n} слогов, лучше 6–10): «{ln.strip()[:40]}…»")
        elif 0 < n < 3 and len(ln.split()) < 2:
            warnings.append(f"Очень короткая строка: «{ln.strip()}»")
    return text, warnings[:8]
