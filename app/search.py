"""Busca com sintaxe estilo wallhaven, estendida.

Termos aceitos no campo de busca (podem ser combinados):

  palavra / "duas palavras"   tag obrigatória (resolve aliases). Se não for tag, busca no título.
  +tag                         tag obrigatória
  -tag                         exclui a tag (e tudo marcado com ela)
  ~tag                         grupo "OU": precisa ter pelo menos uma das tags com ~
  tag:slug                     tag pelo slug exato
  char:slug  / -char:slug      personagem (ou exclui)
  series:slug                  qualquer personagem da série/franquia
  cat:slug   / -cat:slug       categoria
  @usuario                     enviados por um usuário
  color:ff3366                 cor dominante (paleta do site), pode repetir
  ratio:16x9                   proporção exata (pode repetir = OU)
  res:3840x2160                resolução exata
  atleast:3840x2160            resolução mínima
  orientation:portrait         portrait | landscape
  purity:nsfw                  restringe a uma pureza
  like:123                     parecidos com o wallpaper #123 (tags em comum)
  id:123                       um wallpaper específico
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import Select, and_, exists, func, not_, or_, select
from sqlalchemy.orm import Session, selectinload

from .imaging import PALETTE
from .models import (
    PURITIES,
    Category,
    Character,
    Favorite,
    Series,
    User,
    Wallpaper,
    WallpaperCharacter,
    WallpaperColor,
    WallpaperTag,
    utcnow,
)
from .services import resolve_tag
from .utils import slugify

SORTS = {
    "date_added": "Mais recentes",
    "relevance": "Relevância",
    "views": "Mais vistos",
    "favorites": "Mais favoritados",
    "toplist": "Toplist",
    "random": "Aleatório",
}
TOP_RANGES = {"1d": 1, "3d": 3, "1w": 7, "1M": 30, "3M": 90, "6M": 180, "1y": 365}
RES_RE = re.compile(r"^(\d{3,5})x(\d{3,5})$")
COLOR_MIN_PERCENT = 5.0


@dataclass
class ParsedQuery:
    include_tags: list[int] = field(default_factory=list)
    exclude_tags: list[int] = field(default_factory=list)
    any_tags: list[int] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    include_chars: list[int] = field(default_factory=list)
    exclude_chars: list[int] = field(default_factory=list)
    series_ids: list[int] = field(default_factory=list)
    include_cats: list[int] = field(default_factory=list)
    exclude_cats: list[int] = field(default_factory=list)
    uploader_ids: list[int] = field(default_factory=list)
    colors: list[str] = field(default_factory=list)
    ratios: list[str] = field(default_factory=list)
    exact_res: list[tuple[int, int]] = field(default_factory=list)
    atleast: tuple[int, int] | None = None
    orientation: str | None = None
    purity: list[str] = field(default_factory=list)
    like_id: int | None = None
    ids: list[int] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)  # termos que não resolveram (mostrados na UI)
    impossible: bool = False  # algum filtro obrigatório não existe → zero resultados


def normalize_hex(raw: str) -> str | None:
    raw = raw.strip().lower().lstrip("#")
    if re.fullmatch(r"[0-9a-f]{6}", raw):
        hx = f"#{raw}"
        return hx if hx in PALETTE else None
    for hx, name in PALETTE.items():  # aceita nome da cor: color:rosa
        if slugify(name) == slugify(raw):
            return hx
    return None


def parse_query(db: Session, q: str) -> ParsedQuery:
    pq = ParsedQuery()
    try:
        tokens = shlex.split(q or "")
    except ValueError:
        tokens = (q or "").replace('"', " ").split()

    for tok in tokens[:40]:
        tok = tok.strip()
        if not tok:
            continue
        neg = tok.startswith("-")
        body = tok[1:] if tok[0] in "+-~" else tok
        prefix = tok[0] if tok[0] in "+-~" else ""
        key, sep, val = body.partition(":")
        key = key.lower() if sep else ""

        if body.startswith("@"):
            u = db.scalar(select(User).where(func.lower(User.username) == body[1:].lower()))
            if u:
                pq.uploader_ids.append(u.id)
            else:
                pq.unknown.append(tok)
                pq.impossible = True
        elif key in ("char", "character"):
            ch = db.scalar(select(Character).where(Character.slug == slugify(val, 128)))
            if not ch:
                pq.unknown.append(tok)
                pq.impossible |= not neg
            elif neg:
                pq.exclude_chars.append(ch.id)
            else:
                pq.include_chars.append(ch.id)
        elif key == "series":
            s = db.scalar(select(Series).where(Series.slug == slugify(val, 96)))
            if s:
                pq.series_ids.append(s.id)
            else:
                pq.unknown.append(tok)
                pq.impossible = True
        elif key in ("cat", "category"):
            c = db.scalar(select(Category).where(Category.slug == slugify(val)))
            if not c:
                pq.unknown.append(tok)
                pq.impossible |= not neg
            elif neg:
                pq.exclude_cats.append(c.id)
            else:
                pq.include_cats.append(c.id)
        elif key in ("color", "colour", "cor"):
            hx = normalize_hex(val)
            if hx:
                pq.colors.append(hx)
            else:
                pq.unknown.append(tok)
        elif key == "ratio":
            pq.ratios.append(val.lower())
        elif key == "res" and (m := RES_RE.match(val)):
            pq.exact_res.append((int(m[1]), int(m[2])))
        elif key == "atleast" and (m := RES_RE.match(val)):
            pq.atleast = (int(m[1]), int(m[2]))
        elif key == "orientation" and val in ("portrait", "landscape"):
            pq.orientation = val
        elif key == "purity" and val in PURITIES:
            pq.purity.append(val)
        elif key == "like" and val.isdigit():
            pq.like_id = int(val)
        elif key == "id" and val.isdigit():
            pq.ids.append(int(val))
        else:
            raw = val if key == "tag" else body
            tag = resolve_tag(db, raw)
            if tag:
                {"-": pq.exclude_tags, "~": pq.any_tags}.get(prefix, pq.include_tags).append(tag.id)
            elif prefix == "-":
                pq.unknown.append(tok)
            elif prefix == "+" or key == "tag":
                pq.unknown.append(tok)
                pq.impossible = True
            else:
                pq.text.append(raw)
    return pq


@dataclass
class SearchParams:
    q: str = ""
    purity: list[str] = field(default_factory=lambda: list(PURITIES))
    categories: list[int] = field(default_factory=list)
    sorting: str = "date_added"
    order: str = "desc"
    top_range: str = "1M"
    atleast: str = ""
    ratios: list[str] = field(default_factory=list)
    colors: list[str] = field(default_factory=list)
    page: int = 1
    seed: int = 1
    favorites_of: int | None = None
    status: str = "approved"


def build_query(db: Session, params: SearchParams) -> tuple[Select, ParsedQuery]:
    pq = parse_query(db, params.q)
    stmt = select(Wallpaper).where(Wallpaper.status == params.status)

    if pq.impossible:
        return stmt.where(False), pq

    purities = [p for p in params.purity if p in PURITIES]
    if pq.purity:
        purities = [p for p in purities if p in pq.purity] or pq.purity
    stmt = stmt.where(Wallpaper.purity.in_(purities or ["__none__"]))

    for tid in pq.include_tags:
        stmt = stmt.where(exists().where(WallpaperTag.wallpaper_id == Wallpaper.id, WallpaperTag.tag_id == tid))
    if pq.any_tags:
        stmt = stmt.where(
            exists().where(WallpaperTag.wallpaper_id == Wallpaper.id, WallpaperTag.tag_id.in_(pq.any_tags))
        )
    if pq.exclude_tags:
        stmt = stmt.where(
            not_(exists().where(WallpaperTag.wallpaper_id == Wallpaper.id, WallpaperTag.tag_id.in_(pq.exclude_tags)))
        )
    for cid in pq.include_chars:
        stmt = stmt.where(
            exists().where(WallpaperCharacter.wallpaper_id == Wallpaper.id, WallpaperCharacter.character_id == cid)
        )
    if pq.exclude_chars:
        stmt = stmt.where(
            not_(
                exists().where(
                    WallpaperCharacter.wallpaper_id == Wallpaper.id,
                    WallpaperCharacter.character_id.in_(pq.exclude_chars),
                )
            )
        )
    for sid in pq.series_ids:
        stmt = stmt.where(
            exists()
            .where(WallpaperCharacter.wallpaper_id == Wallpaper.id)
            .where(WallpaperCharacter.character_id == Character.id, Character.series_id == sid)
        )
    for word in pq.text:
        stmt = stmt.where(Wallpaper.title.ilike(f"%{word}%"))

    cats = list(dict.fromkeys(pq.include_cats or params.categories))
    if cats:
        stmt = stmt.where(Wallpaper.category_id.in_(cats))
    if pq.exclude_cats:
        stmt = stmt.where(or_(Wallpaper.category_id.is_(None), Wallpaper.category_id.not_in(pq.exclude_cats)))
    if pq.uploader_ids:
        stmt = stmt.where(Wallpaper.uploader_id.in_(pq.uploader_ids))
    if pq.ids:
        stmt = stmt.where(Wallpaper.id.in_(pq.ids))

    colors = list(dict.fromkeys(pq.colors + [c for c in (normalize_hex(x) for x in params.colors) if c]))
    for hx in colors:
        stmt = stmt.where(
            exists().where(
                WallpaperColor.wallpaper_id == Wallpaper.id,
                WallpaperColor.hex == hx,
                WallpaperColor.percent >= COLOR_MIN_PERCENT,
            )
        )

    ratios = pq.ratios + params.ratios
    if ratios:
        conds = []
        for r in ratios:
            if r == "landscape":
                conds.append(Wallpaper.width >= Wallpaper.height)
            elif r == "portrait":
                conds.append(Wallpaper.width < Wallpaper.height)
            else:
                conds.append(Wallpaper.ratio_label == r)
        stmt = stmt.where(or_(*conds))
    if pq.orientation == "portrait":
        stmt = stmt.where(Wallpaper.width < Wallpaper.height)
    elif pq.orientation == "landscape":
        stmt = stmt.where(Wallpaper.width >= Wallpaper.height)
    if pq.exact_res:
        stmt = stmt.where(or_(*(and_(Wallpaper.width == w, Wallpaper.height == h) for w, h in pq.exact_res)))
    atleast = pq.atleast
    if not atleast and (m := RES_RE.match(params.atleast or "")):
        atleast = (int(m[1]), int(m[2]))
    if atleast:
        stmt = stmt.where(Wallpaper.width >= atleast[0], Wallpaper.height >= atleast[1])

    if params.favorites_of:
        stmt = stmt.where(
            exists().where(Favorite.wallpaper_id == Wallpaper.id, Favorite.user_id == params.favorites_of)
        )

    # ------------------------------------------------------------- ordenação
    desc = params.order != "asc"
    like_score = None
    if pq.like_id:
        ref_tags = select(WallpaperTag.tag_id).where(WallpaperTag.wallpaper_id == pq.like_id)
        like_score = (
            select(func.count())
            .select_from(WallpaperTag)
            .where(WallpaperTag.wallpaper_id == Wallpaper.id, WallpaperTag.tag_id.in_(ref_tags))
            .scalar_subquery()
        )
        stmt = stmt.where(Wallpaper.id != pq.like_id, like_score > 0)

    def o(col):
        return col.desc() if desc else col.asc()

    sorting = params.sorting if params.sorting in SORTS else "date_added"
    if sorting == "relevance" and like_score is not None:
        stmt = stmt.order_by(like_score.desc(), Wallpaper.favorites_count.desc())
    elif sorting == "relevance":
        stmt = stmt.order_by(o(Wallpaper.favorites_count * 3 + Wallpaper.views), Wallpaper.id.desc())
    elif sorting == "views":
        stmt = stmt.order_by(o(Wallpaper.views), Wallpaper.id.desc())
    elif sorting == "favorites":
        stmt = stmt.order_by(o(Wallpaper.favorites_count), Wallpaper.id.desc())
    elif sorting == "toplist":
        days = TOP_RANGES.get(params.top_range, 30)
        stmt = stmt.where(Wallpaper.created_at >= utcnow() - timedelta(days=days)).order_by(
            Wallpaper.favorites_count.desc(), Wallpaper.views.desc()
        )
    elif sorting == "random":
        seed = max(1, params.seed % 99991)
        stmt = stmt.order_by((Wallpaper.id * seed + 7919) % 100003)
    elif like_score is not None:
        stmt = stmt.order_by(like_score.desc(), o(Wallpaper.created_at))
    else:
        stmt = stmt.order_by(o(Wallpaper.created_at), o(Wallpaper.id))
    return stmt, pq


def run_search(db: Session, params: SearchParams, per_page: int) -> tuple[list[Wallpaper], int, ParsedQuery]:
    stmt, pq = build_query(db, params)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    page = max(1, params.page)
    rows = db.scalars(
        stmt.options(selectinload(Wallpaper.colors)).limit(per_page).offset((page - 1) * per_page)
    ).all()
    return list(rows), total or 0, pq
