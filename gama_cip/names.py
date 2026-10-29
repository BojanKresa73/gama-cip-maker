"""Serbian-safe names: Cyrillic -> Latin, Latin diacritics -> ASCII for files read by press consoles."""
import re
import unicodedata

CYR = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Ђ": "Đ", "Е": "E", "Ж": "Ž", "З": "Z", "И": "I",
    "Ј": "J", "К": "K", "Л": "L", "Љ": "Lj", "М": "M", "Н": "N", "Њ": "Nj", "О": "O", "П": "P", "Р": "R",
    "С": "S", "Т": "T", "Ћ": "Ć", "У": "U", "Ф": "F", "Х": "H", "Ц": "C", "Ч": "Č", "Џ": "Dž", "Ш": "Š",
}
CYR.update({k.lower(): v.lower() for k, v in list(CYR.items())})
ASCII = {"Đ": "Dj", "đ": "dj", "Ž": "Z", "ž": "z", "Š": "S", "š": "s", "Č": "C", "č": "c", "Ć": "C", "ć": "c"}
WINDOWS_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def latin(text):
    """Serbian Cyrillic to Serbian Latin; everything else unchanged."""
    return "".join(CYR.get(ch, ch) for ch in str(text))


def ascii_name(text, fallback="GAMA", limit=100):
    """File-name-safe ASCII: 'Kutija štanc – Đorđe' -> 'Kutija_stanc_Djordje'."""
    s = "".join(ASCII.get(ch, ch) for ch in latin(text))
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("._")
    s = re.sub(r"_{2,}", "_", s)[:limit].strip("._")
    return s or fallback


def folder_name(text, fallback="Stamparija"):
    """Readable Windows folder name that keeps Serbian letters but drops forbidden characters."""
    s = WINDOWS_FORBIDDEN.sub(" ", str(text)).strip().rstrip(". ")
    s = re.sub(r"\s{2,}", " ", s)[:80].rstrip(". ")
    if not s or s.upper() in RESERVED:
        return fallback
    return s


def job_id_from_name(name, limit=64):
    """Job number from a file name: first stand-alone run of 4+ digits ('..._26527_ed...' -> '26527').

    Digits glued to letters or to 'x' (745x605, p0001) are not job numbers.
    Falls back to the ASCII file name.
    """
    stem = ascii_name(name.rsplit(".", 1)[0] if name.lower().endswith(".pdf") else name)
    match = re.search(r"(?<![0-9A-Za-z])\d{4,}(?![0-9A-Za-z])", stem)
    return match[0] if match else stem[:limit]


BACK_WORDS = re.compile(r"(?<![a-z])(back|nalicje|poledjina)(?![a-z])")
FRONT_WORDS = re.compile(r"(?<![a-z])(front|lice)(?![a-z])")


def side_from_name(name):
    """'Front'/'Back' when the file name says so ('..._back.pdf', 'naličje', 'poleđina'), else None.

    PressPerCent writes every PDF page as Front; Back only makes sense when the
    name explicitly marks the reverse side.
    """
    words = ascii_name(name).lower().replace(".", "_")
    if BACK_WORDS.search(words):
        return "Back"
    if FRONT_WORDS.search(words):
        return "Front"
    return None


def search_key(text):
    """Accent- and script-insensitive key: 'Štanc', 'stanc' and 'Штанц' all match 'stanc'."""
    return ascii_name(text, fallback="", limit=1000).lower().replace("_", " ")
