"""Regras de negócio: tags (aliases + implicações), personagens, contadores e upload."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import imaging
from .models import (
    PURITIES,
    Category,
    Character,
    Series,
    Tag,
    TagAlias,
    TagCategory,
    User,
    Wallpaper,
    WallpaperCharacter,
    WallpaperColor,
    WallpaperTag,
)
from .storage import Storage
from .utils import normalize_tag_name, slugify

MAX_TAGS_PER_WALLPAPER = 60
NEAR_DUPLICATE_DISTANCE = 4  # bits de diferença no dHash


# ------------------------------------------------------------------ tags


def resolve_tag(db: Session, raw: str) -> Tag | None:
    """Encontra a tag canônica por nome, slug ou alias."""
    name = normalize_tag_name(raw)
    if not name:
        return None
    tag = db.scalar(select(Tag).where(Tag.name == name))
    if tag:
        return tag
    tag = db.scalar(select(Tag).where(Tag.slug == slugify(name)))
    if tag:
        return tag
    alias = db.scalar(select(TagAlias).where(TagAlias.alias == name))
    return alias.tag if alias else None


def get_or_create_tag(db: Session, raw: str, user: User | None, purity: str = "sfw") -> Tag | None:
    tag = resolve_tag(db, raw)
    if tag:
        return tag
    name = normalize_tag_name(raw)
    if len(name) < 2:
        return None
    base = slugify(name)
    slug, n = base, 2
    while db.scalar(select(Tag.id).where(Tag.slug == slug)):
        slug, n = f"{base}-{n}", n + 1
    general = db.scalar(select(TagCategory).where(TagCategory.slug == "geral"))
    tag = Tag(
        name=name,
        slug=slug,
        purity=purity if purity in PURITIES else "sfw",
        tag_category=general,
        created_by_id=user.id if user else None,
    )
    db.add(tag)
    db.flush()
    return tag


def implication_closure(tags: list[Tag]) -> set[int]:
    """IDs de todas as tags implicadas (transitivamente) pelas tags dadas."""
    seen: set[int] = {t.id for t in tags}
    implied: set[int] = set()
    stack = list(tags)
    while stack:
        t = stack.pop()
        for imp in t.implies:
            if imp.id not in seen:
                seen.add(imp.id)
                implied.add(imp.id)
                stack.append(imp)
    return implied


def set_wallpaper_tags(db: Session, wp: Wallpaper, names: list[str], user: User | None) -> None:
    explicit: dict[int, Tag] = {}
    for raw in names[:MAX_TAGS_PER_WALLPAPER]:
        tag = get_or_create_tag(db, raw, user)
        if tag:
            explicit[tag.id] = tag
    implied_ids = implication_closure(list(explicit.values())) - set(explicit)

    current = {link.tag_id: link for link in wp.tag_links}
    wanted = set(explicit) | implied_ids
    for tag_id, link in list(current.items()):
        if tag_id not in wanted:
            wp.tag_links.remove(link)
    for tag_id in wanted:
        if tag_id in current:
            current[tag_id].implied = tag_id in implied_ids
        else:
            wp.tag_links.append(
                WallpaperTag(tag_id=tag_id, added_by_id=user.id if user else None, implied=tag_id in implied_ids)
            )
    db.flush()
    refresh_tag_counts(db, list(wanted | set(current)))


def refresh_tag_counts(db: Session, tag_ids: list[int] | None = None) -> None:
    q = select(Tag)
    if tag_ids is not None:
        if not tag_ids:
            return
        q = q.where(Tag.id.in_(tag_ids))
    for tag in db.scalars(q):
        tag.usage_count = db.scalar(
            select(func.count())
            .select_from(WallpaperTag)
            .join(Wallpaper)
            .where(WallpaperTag.tag_id == tag.id, Wallpaper.status == "approved")
        )


def add_implication(db: Session, tag: Tag, implied: Tag) -> str | None:
    if tag.id == implied.id:
        return "Uma tag não pode implicar ela mesma."
    if tag.id in implication_closure([implied]):
        return "Isso criaria um ciclo de implicações."
    if implied not in tag.implies:
        tag.implies.append(implied)
    return None


# ------------------------------------------------------------------ personagens


def resolve_character(db: Session, raw: str) -> Character | None:
    raw = raw.strip()
    if not raw:
        return None
    ch = db.scalar(select(Character).where(Character.slug == slugify(raw, 128)))
    if ch:
        return ch
    ch = db.scalar(select(Character).where(func.lower(Character.name) == raw.lower()))
    if ch:
        return ch
    low = raw.lower()
    for c in db.scalars(select(Character).where(Character.aliases.ilike(f"%{raw}%"))):
        if low in (a.lower() for a in c.alias_list):
            return c
    return None


def get_or_create_character(db: Session, raw: str) -> Character | None:
    """Aceita 'Nome' ou 'Nome (Série)'."""
    raw = raw.strip()[:96]
    if len(raw) < 2:
        return None
    ch = resolve_character(db, raw)
    if ch:
        return ch
    name, series = raw, None
    if raw.endswith(")") and "(" in raw:
        name, _, rest = raw.rpartition("(")
        name, series_name = name.strip(), rest[:-1].strip()
        if series_name:
            series = db.scalar(select(Series).where(func.lower(Series.name) == series_name.lower()))
            if not series:
                series = Series(name=series_name, slug=slugify(series_name, 96))
                db.add(series)
                db.flush()
    slug_base = slugify(f"{name} {series.name}" if series else name, 128)
    slug, n = slug_base, 2
    while db.scalar(select(Character.id).where(Character.slug == slug)):
        slug, n = f"{slug_base}-{n}", n + 1
    ch = Character(name=name, slug=slug, series=series)
    db.add(ch)
    db.flush()
    return ch


def set_wallpaper_characters(db: Session, wp: Wallpaper, names: list[str]) -> None:
    wanted: dict[int, Character] = {}
    for raw in names[:20]:
        ch = get_or_create_character(db, raw)
        if ch:
            wanted[ch.id] = ch
    touched = {link.character_id for link in wp.character_links} | set(wanted)
    for link in list(wp.character_links):
        if link.character_id not in wanted:
            wp.character_links.remove(link)
    have = {link.character_id for link in wp.character_links}
    for cid in wanted:
        if cid not in have:
            wp.character_links.append(WallpaperCharacter(character_id=cid))
    db.flush()
    refresh_character_counts(db, list(touched))


def refresh_character_counts(db: Session, ids: list[int]) -> None:
    if not ids:
        return
    for ch in db.scalars(select(Character).where(Character.id.in_(ids))):
        ch.usage_count = db.scalar(
            select(func.count())
            .select_from(WallpaperCharacter)
            .join(Wallpaper)
            .where(WallpaperCharacter.character_id == ch.id, Wallpaper.status == "approved")
        )


# ------------------------------------------------------------------ upload


class UploadError(ValueError):
    pass


def find_near_duplicate(db: Session, dh: str) -> Wallpaper | None:
    # prefiltro barato pelos 4 primeiros hex não é confiável p/ hamming, então
    # comparamos contra todos os hashes (rápido até centenas de milhares de linhas).
    for wid, other in db.execute(select(Wallpaper.id, Wallpaper.dhash).where(Wallpaper.status != "rejected")):
        if imaging.hamming(dh, other) <= NEAR_DUPLICATE_DISTANCE:
            return db.get(Wallpaper, wid)
    return None


def split_list(raw: str) -> list[str]:
    seen, out = set(), []
    for part in raw.replace("\n", ",").split(","):
        p = part.strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


async def create_wallpaper(
    db: Session,
    storage: Storage,
    user: User,
    data: bytes,
    *,
    category_id: int | None,
    purity: str,
    title: str = "",
    source: str = "",
    tags: list[str] | None = None,
    characters: list[str] | None = None,
) -> Wallpaper:
    try:
        img = imaging.process_upload(data)
    except imaging.ImageRejected as e:
        raise UploadError(str(e)) from None

    dup = db.scalar(select(Wallpaper).where(Wallpaper.sha256 == img.sha256))
    if dup:
        raise UploadError(f"Este wallpaper já existe no site (#{dup.id}).")
    near = find_near_duplicate(db, img.dhash)
    if near:
        raise UploadError(f"Imagem praticamente idêntica ao wallpaper #{near.id} já enviado.")

    if purity not in PURITIES:
        purity = "sketchy"
    category = db.get(Category, category_id) if category_id else None

    caption = f"High · {user.username} · {img.width}x{img.height} · {img.sha256[:12]}"
    original = await storage.put(img.data, f"{img.sha256[:16]}.{img.ext}", img.mime, caption)
    try:
        thumb = await storage.put(img.thumb, f"{img.sha256[:16]}_thumb.webp", "image/webp", caption + " · thumb")
    except Exception:
        await storage.delete(original.ref, original.message_id)
        raise

    wp = Wallpaper(
        uploader_id=user.id,
        category=category,
        title=title.strip()[:140],
        source=source.strip()[:500],
        purity=purity,
        status="approved" if user.is_staff else "pending",
        width=img.width,
        height=img.height,
        ratio=img.ratio,
        ratio_label=img.ratio_label,
        file_size=len(img.data),
        mime=img.mime,
        sha256=img.sha256,
        dhash=img.dhash,
        storage_backend=storage.name,
        file_ref=original.ref,
        file_message_id=original.message_id,
        thumb_ref=thumb.ref,
        thumb_message_id=thumb.message_id,
        colors=[WallpaperColor(hex=h, percent=p) for h, p in img.colors],
    )
    db.add(wp)
    db.flush()
    set_wallpaper_tags(db, wp, tags or [], user)
    set_wallpaper_characters(db, wp, characters or [])
    db.commit()
    return wp


def refresh_all_counts(db: Session, wp: Wallpaper) -> None:
    db.flush()
    refresh_tag_counts(db, [link.tag_id for link in wp.tag_links])
    refresh_character_counts(db, [link.character_id for link in wp.character_links])
