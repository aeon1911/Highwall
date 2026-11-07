"""Validação, miniaturas, extração de cores e hash perceptual das imagens."""

from __future__ import annotations

import hashlib
import io
import struct
from collections import Counter
from dataclasses import dataclass, field
from math import gcd

from PIL import Image, ImageOps

from .config import get_settings

# Proteção contra "decompression bombs": ~16K x 10K é mais que suficiente para wallpaper.
Image.MAX_IMAGE_PIXELS = 170_000_000

ALLOWED_FORMATS = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "WEBP": ("image/webp", "webp")}

# Paleta fixa do site. As cores de cada wallpaper são "encaixadas" nela,
# o que torna a busca por cor previsível (igual ao wallhaven, com mais tons de pele/neon).
PALETTE: dict[str, str] = {
    "#660000": "vinho", "#990000": "carmim", "#cc0000": "vermelho", "#ff3366": "rosa choque",
    "#ff6699": "rosa", "#ffb3c6": "rosa claro", "#993399": "magenta", "#663399": "roxo",
    "#9966cc": "lavanda", "#330066": "índigo", "#000066": "marinho", "#003399": "azul",
    "#3399ff": "azul céu", "#66ccff": "ciano", "#00cccc": "turquesa", "#006666": "petróleo",
    "#336600": "verde musgo", "#339933": "verde", "#99cc33": "lima", "#ffff00": "amarelo",
    "#ffcc33": "dourado", "#ff9900": "laranja", "#cc6633": "cobre", "#663300": "marrom",
    "#996633": "caramelo", "#e8b896": "pele clara", "#c68642": "pele média", "#8d5524": "pele escura",
    "#f5e6d3": "creme", "#ffffff": "branco", "#cccccc": "prata", "#999999": "cinza",
    "#424153": "grafite", "#000000": "preto",
}
_PALETTE_RGB = [(h, tuple(int(h[i : i + 2], 16) for i in (1, 3, 5))) for h in PALETTE]

COMMON_RATIOS = {
    "16x9": 16 / 9, "16x10": 16 / 10, "21x9": 21 / 9, "32x9": 32 / 9, "48x9": 48 / 9,
    "4x3": 4 / 3, "5x4": 5 / 4, "3x2": 3 / 2, "1x1": 1.0,
    "9x16": 9 / 16, "10x16": 10 / 16, "9x18": 9 / 18, "9x19.5": 9 / 19.5, "9x20": 9 / 20,
    "3x4": 3 / 4, "2x3": 2 / 3,
}


class ImageRejected(ValueError):
    pass


@dataclass
class ProcessedImage:
    data: bytes  # bytes finais armazenados (original sem metadados sensíveis)
    mime: str
    ext: str
    width: int
    height: int
    ratio: float
    ratio_label: str
    sha256: str
    dhash: str
    thumb: bytes
    colors: list[tuple[str, float]] = field(default_factory=list)


def ratio_label(w: int, h: int) -> str:
    r = w / h
    best = min(COMMON_RATIOS.items(), key=lambda kv: abs(kv[1] - r))
    if abs(best[1] - r) / r < 0.02:
        return best[0]
    g = gcd(w, h)
    return f"{w // g}x{h // g}"[:12]


def meets_min_resolution(w: int, h: int) -> bool:
    s = get_settings()
    return max(w, h) >= s.min_long_side and min(w, h) >= s.min_short_side


def _redmean(c1, c2) -> float:
    r = (c1[0] + c2[0]) / 2
    dr, dg, db = c1[0] - c2[0], c1[1] - c2[1], c1[2] - c2[2]
    return (2 + r / 256) * dr * dr + 4 * dg * dg + (2 + (255 - r) / 256) * db * db


def nearest_palette(rgb: tuple[int, int, int]) -> str:
    return min(_PALETTE_RGB, key=lambda p: _redmean(rgb, p[1]))[0]


def extract_colors(img: Image.Image, top: int = 5, min_percent: float = 3.0) -> list[tuple[str, float]]:
    small = img.convert("RGB").resize((96, 96), Image.Resampling.BILINEAR)
    # pré-quantiza para 48 cores (rápido) e depois mapeia cada uma para a paleta do site
    q = small.quantize(colors=48, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()
    counts: Counter[str] = Counter()
    for count, idx in q.getcolors():
        rgb = tuple(pal[idx * 3 : idx * 3 + 3])
        counts[nearest_palette(rgb)] += count
    total = sum(counts.values())
    out = [(hx, round(100 * n / total, 2)) for hx, n in counts.most_common(top)]
    return [c for c in out if c[1] >= min_percent] or out[:1]


def dhash(img: Image.Image, size: int = 8) -> str:
    g = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = g.tobytes()
    bits = 0
    for row in range(size):
        for col in range(size):
            bits = (bits << 1) | (px[row * (size + 1) + col] > px[row * (size + 1) + col + 1])
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def make_thumb(img: Image.Image, width: int) -> bytes:
    t = img.convert("RGB")
    h = max(1, round(t.height * width / t.width))
    t = t.resize((width, h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    t.save(buf, "WEBP", quality=82, method=5)
    return buf.getvalue()


# ---------------------------------------------------------------- remoção de metadados


def _strip_jpeg(data: bytes) -> bytes:
    """Remove segmentos APP1..APP15 e COM (EXIF/GPS/XMP), mantendo APP0 e ICC (APP2 ICC_PROFILE)."""
    if data[:2] != b"\xff\xd8":
        return data
    out, i = bytearray(b"\xff\xd8"), 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return data  # estrutura inesperada: devolve original
        marker = data[i + 1]
        if marker == 0xDA:  # início dos dados da imagem: copia o resto
            out += data[i:]
            return bytes(out)
        seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
        seg = data[i : i + 2 + seg_len]
        keep_icc = marker == 0xE2 and seg[4:16] == b"ICC_PROFILE\x00"
        if (0xE1 <= marker <= 0xEF and not keep_icc) or marker == 0xFE:
            pass
        else:
            out += seg
        i += 2 + seg_len
    return data


_PNG_DROP = {b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME"}


def _strip_png(data: bytes) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(sig):
        return data
    out, i = bytearray(sig), 8
    while i + 8 <= len(data):
        length = struct.unpack(">I", data[i : i + 4])[0]
        ctype = data[i + 4 : i + 8]
        chunk = data[i : i + 12 + length]
        if ctype not in _PNG_DROP:
            out += chunk
        i += 12 + length
        if ctype == b"IEND":
            break
    return bytes(out)


# ---------------------------------------------------------------- pipeline principal


def process_upload(data: bytes) -> ProcessedImage:
    s = get_settings()
    if len(data) > s.max_upload_bytes:
        raise ImageRejected(f"Arquivo maior que {s.max_upload_mb} MB.")
    try:
        probe = Image.open(io.BytesIO(data))
        fmt = probe.format
        probe.verify()
    except Image.DecompressionBombError:
        raise ImageRejected("Imagem grande demais (limite de pixels excedido).")
    except Exception:
        raise ImageRejected("Arquivo não é uma imagem válida.")
    if fmt not in ALLOWED_FORMATS:
        raise ImageRejected("Formato não suportado. Use JPG, PNG ou WEBP.")

    img = Image.open(io.BytesIO(data))
    if getattr(img, "is_animated", False):
        raise ImageRejected("Imagens animadas não são aceitas.")
    img.load()

    orientation = img.getexif().get(0x0112, 1)
    mime, ext = ALLOWED_FORMATS[fmt]
    if orientation not in (1, None):
        # imagem rotacionada via EXIF: aplica a rotação e regrava em alta qualidade
        img = ImageOps.exif_transpose(img)
        buf = io.BytesIO()
        if fmt == "JPEG":
            img.convert("RGB").save(buf, "JPEG", quality=95, subsampling=0, icc_profile=img.info.get("icc_profile"))
        else:
            img.save(buf, fmt)
        clean = buf.getvalue()
    elif fmt == "JPEG":
        clean = _strip_jpeg(data)
    elif fmt == "PNG":
        clean = _strip_png(data)
    else:
        clean = data

    w, h = img.size
    if not meets_min_resolution(w, h):
        raise ImageRejected(
            f"Resolução {w}x{h} abaixo do mínimo 2K "
            f"({s.min_long_side}x{s.min_short_side} ou {s.min_short_side}x{s.min_long_side})."
        )

    return ProcessedImage(
        data=clean,
        mime=mime,
        ext=ext,
        width=w,
        height=h,
        ratio=round(w / h, 4),
        ratio_label=ratio_label(w, h),
        sha256=hashlib.sha256(clean).hexdigest(),
        dhash=dhash(img),
        thumb=make_thumb(img, s.thumb_width),
        colors=extract_colors(img),
    )
