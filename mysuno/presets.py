"""Словарь жанров, настроений и типов вокала: русская подпись → английские теги для ACE-Step."""
from __future__ import annotations

# (id, подпись в UI, английские теги)
GENRES: list[tuple[str, str, str]] = [
    ("pop", "Pop", "pop, catchy melody"),
    ("ruspop", "Русская эстрада", "russian pop, estrada"),
    ("sovpop", "Советская эстрада", "soviet pop, 1970s estrada, vintage orchestral pop, warm brass and strings"),
    ("rock", "Rock", "rock, electric guitars, live drums"),
    ("hardrock", "Hard rock", "hard rock, distorted guitars, powerful drums"),
    ("metal", "Metal", "heavy metal, aggressive guitars, double bass drums"),
    ("punk", "Punk", "punk rock, fast, raw energy"),
    ("grunge", "Grunge", "grunge, 90s alternative rock, distorted guitars, raw vocals"),
    ("indie", "Indie", "indie rock, jangly guitars"),
    ("hiphop", "Hip-hop", "hip hop, boom bap beat"),
    ("rap", "Rap", "rap, rhythmic rap vocals"),
    ("trap", "Trap", "trap, 808 bass, rolling hi-hats"),
    ("rnb", "R&B", "r&b, smooth groove"),
    ("soul", "Soul", "soul, warm organ, groovy bass"),
    ("funk", "Funk", "funk, slap bass, groovy rhythm"),
    ("jazz", "Jazz", "jazz, piano, double bass, brushed drums"),
    ("blues", "Blues", "blues, electric guitar, slow shuffle"),
    ("classical", "Классика", "classical, orchestral, strings"),
    ("symphonic", "Симфонический оркестр", "symphony orchestra, full orchestral, strings, brass, woodwinds, timpani"),
    ("neoclassical", "Неоклассика", "neoclassical, modern classical, piano and strings, minimalist"),
    ("impressionist", "Импрессионизм", "impressionistic piano, debussy style, lush harmonies, flowing arpeggios"),
    ("cinematic", "Кино-оркестр", "cinematic orchestral, epic, strings and brass"),
    ("oldfilm", "Старое кино", "vintage film score, 1950s movie soundtrack, old orchestra, nostalgic"),
    ("ghibli", "Музыка Гибли", "studio ghibli style, whimsical orchestral, piano, strings, nostalgic"),
    ("pastoral", "Пастораль", "pastoral, flute, gentle strings, idyllic countryside"),
    ("electronic", "Электроника", "electronic, synthesizers"),
    ("edm", "EDM", "edm, festival, big drop"),
    ("house", "House", "house, four on the floor, club"),
    ("techno", "Techno", "techno, driving, hypnotic"),
    ("trance", "Trance", "trance, uplifting, arpeggios"),
    ("dnb", "Drum and bass", "drum and bass, fast breakbeats, heavy bass"),
    ("dubstep", "Dubstep", "dubstep, wobble bass"),
    ("synthwave", "Synthwave", "synthwave, retro 80s synths"),
    ("newwave", "New wave", "new wave, 80s, post-punk, synth pop, chorus guitars"),
    ("lofi", "Lo-fi", "lo-fi hip hop, chill, vinyl crackle"),
    ("ambient", "Ambient", "ambient, atmospheric pads, slow"),
    ("country", "Country", "country, acoustic guitar, pedal steel"),
    ("folk", "Folk", "folk, acoustic guitar"),
    ("acoustic", "Акустика", "acoustic, unplugged, guitar"),
    ("reggae", "Reggae", "reggae, offbeat guitar, dub bass"),
    ("latin", "Latin", "latin, percussion, rhythmic"),
    ("bossa", "Bossa nova", "bossa nova, nylon guitar, soft"),
    ("disco", "Disco", "disco, funky bass, strings"),
    ("kpop", "K-pop", "k-pop, polished, dance"),
    ("chanson", "Русский шансон", "russian chanson, acoustic guitar, storytelling"),
    ("frchanson", "Французский шансон", "french chanson, accordion, parisian cafe"),
    ("romance", "Романс", "russian romance, classical guitar, piano, heartfelt vocals"),
    ("ballad", "Баллада", "ballad, emotional, slow tempo"),
    ("gospel", "Gospel", "gospel, choir, organ"),
    ("gregorian", "Григорианское пение", "gregorian chant, monastic male choir, latin plainchant, cathedral reverb"),
]

# Русские названия жанров с латинской подписью — для поиска («рок» находит Rock)
GENRE_ALIASES: dict[str, str] = {
    "pop": "поп", "rock": "рок", "hardrock": "хард-рок", "metal": "метал", "punk": "панк", "grunge": "гранж",
    "indie": "инди", "hiphop": "хип-хоп", "rap": "рэп", "trap": "трэп", "rnb": "ар-н-би", "soul": "соул",
    "funk": "фанк", "jazz": "джаз", "blues": "блюз", "edm": "электронная танцевальная", "house": "хаус",
    "techno": "техно", "trance": "транс", "dnb": "драм-н-бейс", "dubstep": "дабстеп", "synthwave": "синтвейв", "newwave": "нью-вейв новая волна",
    "lofi": "лоу-фай", "ambient": "эмбиент", "country": "кантри", "folk": "фолк", "reggae": "регги",
    "latin": "латино", "bossa": "босса-нова", "disco": "диско", "kpop": "кей-поп", "gospel": "госпел",
}

# Жанры, в которых «зашит» язык вокала (код языка ACE); остальные сэмплы жанров поются по-английски
GENRE_LANGUAGE: dict[str, str] = {
    "ruspop": "ru", "sovpop": "ru", "chanson": "ru", "romance": "ru",
    "frchanson": "fr", "latin": "es", "bossa": "pt", "kpop": "ko", "ghibli": "ja", "gregorian": "la",
}

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
_HEAVY = ["metal", "hardrock", "punk", "grunge", "dnb", "dubstep", "trap", "edm"]
GENRE_CONFLICTS: list[tuple[str, str]] = (
    [("ambient", g) for g in _HEAVY] + [("classical", g) for g in _HEAVY] + [("lofi", g) for g in _HEAVY]
    + [("bossa", g) for g in _HEAVY]
    + [(a, g) for a in ("neoclassical", "impressionist", "ghibli", "pastoral", "romance", "symphonic", "oldfilm", "gregorian") for g in _HEAVY] + [("acoustic", g) for g in ("dubstep", "edm", "techno", "dnb", "metal")]
    + [("folk", g) for g in ("dubstep", "edm", "dnb", "metal")]
)
MAX_GENRES = 3


def presets_payload() -> dict:
    """Данные для UI."""
    return {
        "genres": [{"id": i, "label": l, "alias": GENRE_ALIASES.get(i, "")} for i, l, _ in GENRES],
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
