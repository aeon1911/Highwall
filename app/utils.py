import re
import unicodedata


def slugify(text: str, max_len: int = 64) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text[:max_len].strip("-") or "item"


def normalize_tag_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name.strip().lower())
    return name[:64]


def clamp_int(value, default: int, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))
