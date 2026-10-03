"""Словарь жанров, настроений и типов вокала: русская подпись → английские теги для ACE-Step."""
from __future__ import annotations

# (id, подпись в UI, английские теги)
GENRES: list[tuple[str, str, str]] = [
    ("pop", "Поп", "pop, catchy melody"),
    ("ruspop", "Русская эстрада", "russian pop, estrada"),
    ("rock", "Рок", "rock, electric guitars, live drums"),
    ("hardrock", "Хард-рок", "hard rock, distorted guitars, powerful drums"),
    ("metal", "Метал", "heavy metal, aggressive guitars, double bass drums"),
    ("punk", "Панк", "punk rock, fast, raw energy"),
    ("indie", "Инди", "indie rock, jangly guitars"),
    ("hiphop", "Хип-хоп", "hip hop, boom bap beat"),
    ("rap", "Рэп", "rap, rhythmic rap vocals"),
    ("trap", "Трэп", "trap, 808 bass, rolling hi-hats"),
    ("rnb", "R&B", "r&b, smooth groove"),
    ("soul", "Соул", "soul, warm organ, groovy bass"),
    ("funk", "Фанк", "funk, slap bass, groovy rhythm"),
    ("jazz", "Джаз", "jazz, piano, double bass, brushed drums"),
    ("blues", "Блюз", "blues, electric guitar, slow shuffle"),
    ("classical", "Классика", "classical, orchestral, strings"),
    ("neoclassical", "Неоклассика", "neoclassical, modern classical, piano and strings, minimalist"),
    ("impressionist", "Импрессионизм", "impressionistic piano, debussy style, lush harmonies, flowing arpeggios"),
    ("cinematic", "Кино-оркестр", "cinematic orchestral, epic, strings and brass"),
    ("ghibli", "Музыка Гибли", "studio ghibli style, whimsical orchestral, piano, strings, nostalgic"),
    ("pastoral", "Пастораль", "pastoral, flute, gentle strings, idyllic countryside"),
    ("electronic", "Электроника", "electronic, synthesizers"),
    ("edm", "EDM", "edm, festival, big drop"),
    ("house", "Хаус", "house, four on the floor, club"),
    ("techno", "Техно", "techno, driving, hypnotic"),
    ("trance", "Транс", "trance, uplifting, arpeggios"),
    ("dnb", "Драм-н-бейс", "drum and bass, fast breakbeats, heavy bass"),
    ("dubstep", "Дабстеп", "dubstep, wobble bass"),
    ("synthwave", "Синтвейв", "synthwave, retro 80s synths"),
    ("lofi", "Лоу-фай", "lo-fi hip hop, chill, vinyl crackle"),
    ("ambient", "Эмбиент", "ambient, atmospheric pads, slow"),
    ("country", "Кантри", "country, acoustic guitar, pedal steel"),
    ("folk", "Фолк", "folk, acoustic guitar"),
    ("acoustic", "Акустика", "acoustic, unplugged, guitar"),
    ("reggae", "Регги", "reggae, offbeat guitar, dub bass"),
    ("latin", "Латино", "latin, percussion, rhythmic"),
    ("bossa", "Босса-нова", "bossa nova, nylon guitar, soft"),
    ("disco", "Диско", "disco, funky bass, strings"),
    ("kpop", "K-pop", "k-pop, polished, dance"),
    ("chanson", "Русский шансон", "russian chanson, acoustic guitar, storytelling"),
    ("frchanson", "Французский шансон", "french chanson, accordion, parisian cafe"),
    ("romance", "Романс", "russian romance, classical guitar, piano, heartfelt vocals"),
    ("ballad", "Баллада", "ballad, emotional, slow tempo"),
    ("gospel", "Госпел", "gospel, choir, organ"),
]

MOODS: list[tuple[str, str, str]] = [
    ("sad", "Грустное", "sad, melancholic"),
    ("happy", "Весёлое", "happy, upbeat"),
    ("energetic", "Энергичное", "energetic, powerful"),
    ("calm", "Спокойное", "calm, relaxing"),
    ("romantic", "Романтичное", "romantic, tender"),
    ("epic", "Эпичное", "epic, grand"),
    ("dark", "Мрачное", "dark, moody"),
    ("dreamy", "Мечтательное", "dreamy, ethereal"),
    ("nostalgic", "Ностальгическое", "nostalgic, warm"),
]

# id → (подпись, теги, инструментал)
VOCALS: list[tuple[str, str, str, bool]] = [
    ("auto", "Авто", "", False),
    ("male", "Мужской", "male vocals, male singer", False),
    ("female", "Женский", "female vocals, female singer", False),
    ("child", "Детский", "child vocals, children's voice, young kids singing", False),
    ("choir", "Хор", "choir, choral vocals, group vocals", False),
    ("duet", "Дуэт", "male and female duet vocals", False),
    ("none", "Без вокала", "", True),
]

_GENRE_TAGS = {g[0]: g[2] for g in GENRES}
_MOOD_TAGS = {m[0]: m[2] for m in MOODS}
_VOCAL = {v[0]: v for v in VOCALS}

CAPTION_LIMIT = 500

# ACE-Step: «модель плохо разрешает конфликты» между стилями/настроениями — подсказываем пользователю заранее.
MOOD_CONFLICTS: list[tuple[str, str]] = [
    ("sad", "happy"), ("dark", "happy"), ("calm", "energetic"), ("calm", "epic"),
    ("dreamy", "energetic"), ("sad", "energetic"),
]
_HEAVY = ["metal", "hardrock", "punk", "dnb", "dubstep", "trap", "edm"]
GENRE_CONFLICTS: list[tuple[str, str]] = (
    [("ambient", g) for g in _HEAVY] + [("classical", g) for g in _HEAVY] + [("lofi", g) for g in _HEAVY]
    + [("bossa", g) for g in _HEAVY]
    + [(a, g) for a in ("neoclassical", "impressionist", "ghibli", "pastoral", "romance") for g in _HEAVY] + [("acoustic", g) for g in ("dubstep", "edm", "techno", "dnb", "metal")]
    + [("folk", g) for g in ("dubstep", "edm", "dnb", "metal")]
)
MAX_GENRES = 3


def presets_payload() -> dict:
    """Данные для UI."""
    return {
        "genres": [{"id": i, "label": l} for i, l, _ in GENRES],
        "moods": [{"id": i, "label": l} for i, l, _ in MOODS],
        "vocals": [{"id": i, "label": l, "instrumental": inst} for i, l, _, inst in VOCALS],
        "conflicts": {"genres": GENRE_CONFLICTS, "moods": MOOD_CONFLICTS, "max_genres": MAX_GENRES},
    }


def check_conflicts(genres: list[str], moods: list[str]) -> list[str]:
    """Человекочитаемые предупреждения о противоречивых или избыточных тегах."""
    glabel = {i: l for i, l, _ in GENRES}
    mlabel = {i: l for i, l, _ in MOODS}
    out = []
    for a, b in GENRE_CONFLICTS:
        if a in genres and b in genres:
            out.append(f"Жанры «{glabel[a]}» и «{glabel[b]}» плохо сочетаются — модель смешает их в кашу")
    for a, b in MOOD_CONFLICTS:
        if a in moods and b in moods:
            out.append(f"Настроения «{mlabel[a]}» и «{mlabel[b]}» противоречат друг другу")
    if len(genres) > MAX_GENRES:
        out.append(f"Выбрано жанров: {len(genres)} — надёжнее не больше {MAX_GENRES}")
    return out


def is_instrumental(vocal: str) -> bool:
    return bool(_VOCAL.get(vocal, _VOCAL["auto"])[3])


def build_caption(
    genres: list[str],
    moods: list[str],
    vocal: str,
    description_en: str = "",
    negative_genres: list[str] | None = None,
) -> tuple[str, str]:
    """Собирает английский caption и негативный промпт для LM.

    Возвращает (caption, negative_prompt). Жанры, попавшие в негативные, из позитивных
    убираются. negative_prompt = "NO USER INPUT", если негативных жанров нет.
    """
    negatives = [g for g in (negative_genres or []) if g in _GENRE_TAGS]
    positives = [g for g in genres if g in _GENRE_TAGS and g not in negatives]

    parts: list[str] = [_GENRE_TAGS[g] for g in positives]
    parts += [_MOOD_TAGS[m] for m in moods if m in _MOOD_TAGS]
    vocal_tags = _VOCAL.get(vocal, _VOCAL["auto"])[2]
    if vocal_tags:
        parts.append(vocal_tags)
    if description_en.strip():
        parts.append(description_en.strip())

    caption = ", ".join(parts)
    if len(caption) > CAPTION_LIMIT:
        caption = caption[:CAPTION_LIMIT].rsplit(",", 1)[0]

    if negatives:
        negative = ", ".join(_GENRE_TAGS[g].split(",")[0].strip() for g in negatives)
    else:
        negative = "NO USER INPUT"
    return caption, negative
