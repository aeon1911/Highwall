from __future__ import annotations

import random
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..db import get_db
from ..models import (
    PURITIES,
    Category,
    Character,
    Favorite,
    Series,
    Tag,
    TagCategory,
    User,
    Wallpaper,
    WallpaperCharacter,
    WallpaperColor,
    WallpaperTag,
)
from ..search import SearchParams, run_search
from ..security import current_user
from ..utils import clamp_int
from ..web import render

router = APIRouter()


def allowed_purities(user: User | None) -> list[str]:
    """Visitantes não logados não veem conteúdo explícito."""
    if user is None:
        return ["sfw", "sketchy"]
    return list(PURITIES)


def params_from_request(request: Request, user: User | None, **override) -> SearchParams:
    qp = request.query_params
    allowed = allowed_purities(user)
    if "purity" in qp:
        purity = [p for p in qp.getlist("purity") if p in allowed]
    else:
        pref = user.purity_pref.split(",") if user else allowed
        purity = [p for p in pref if p in allowed]
    p = SearchParams(
        q=qp.get("q", "")[:500],
        purity=purity,
        categories=[int(c) for c in qp.getlist("cat") if c.isdigit()],
        sorting=qp.get("sorting", "date_added"),
        order=qp.get("order", "desc"),
        top_range=qp.get("range", "1M"),
        atleast=qp.get("atleast", ""),
        ratios=qp.getlist("ratio"),
        colors=qp.getlist("color"),
        page=clamp_int(qp.get("page"), 1, 1, 10_000),
        seed=clamp_int(qp.get("seed"), random.randint(1, 99990), 1, 99990),
    )
    for k, v in override.items():
        setattr(p, k, v)
    return p


def search_context(db: Session, request: Request, user: User | None, params: SearchParams, **extra) -> dict:
    per_page = get_settings().per_page
    items, total, pq = run_search(db, params, per_page)
    fav_ids = set()
    if user and items:
        fav_ids = set(
            db.scalars(
                select(Favorite.wallpaper_id).where(
                    Favorite.user_id == user.id, Favorite.wallpaper_id.in_([w.id for w in items])
                )
            )
        )
    query = {
        "q": params.q,
        "purity": params.purity,
        "cat": params.categories,
        "sorting": params.sorting,
        "order": params.order,
        "range": params.top_range if params.sorting == "toplist" else None,
        "atleast": params.atleast,
        "ratio": params.ratios,
        "color": params.colors,
        "seed": params.seed if params.sorting == "random" else None,
    }
    return {
        "items": items,
        "total": total,
        "page": params.page,
        "pages": max(1, ceil(total / per_page)),
        "params": params,
        "query": query,
        "parsed": pq,
        "fav_ids": fav_ids,
        "categories": db.scalars(select(Category).order_by(Category.sort_order)).all(),
        "allowed_purities": allowed_purities(user),
        **extra,
    }


@router.get("/")
def home(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    allowed = allowed_purities(user)
    pref = [p for p in (user.purity_pref.split(",") if user else allowed) if p in allowed]
    latest, _, _ = run_search(db, SearchParams(purity=pref), 12)
    top, _, _ = run_search(db, SearchParams(purity=pref, sorting="toplist", top_range="1M"), 6)
    categories = db.scalars(select(Category).options(selectinload(Category.colors)).order_by(Category.sort_order)).all()
    popular_tags = db.scalars(select(Tag).where(Tag.usage_count > 0).order_by(Tag.usage_count.desc()).limit(30)).all()
    stats = {
        "wallpapers": db.scalar(select(func.count()).select_from(Wallpaper).where(Wallpaper.status == "approved")),
        "tags": db.scalar(select(func.count()).select_from(Tag)),
        "users": db.scalar(select(func.count()).select_from(User)),
    }
    return render(
        request,
        "home.html",
        {"latest": latest, "top": top, "categories": categories, "popular_tags": popular_tags, "stats": stats},
    )


@router.get("/search")
def search(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    params = params_from_request(request, user)
    return render(request, "search.html", search_context(db, request, user, params, title="Busca"))


@router.get("/latest")
def latest(request: Request):
    return RedirectResponse("/search?sorting=date_added", status_code=303)


@router.get("/toplist")
def toplist(request: Request):
    return RedirectResponse("/search?sorting=toplist&range=1M", status_code=303)


@router.get("/random")
def random_page(request: Request):
    return RedirectResponse(f"/search?sorting=random&seed={random.randint(1, 99990)}", status_code=303)


# ------------------------------------------------------------------ wallpaper


def load_wallpaper(db: Session, wid: int, user: User | None) -> Wallpaper:
    wp = db.scalar(
        select(Wallpaper)
        .where(Wallpaper.id == wid)
        .options(
            selectinload(Wallpaper.tag_links).selectinload(WallpaperTag.tag).selectinload(Tag.tag_category),
            selectinload(Wallpaper.character_links).selectinload(WallpaperCharacter.character),
            selectinload(Wallpaper.colors),
            selectinload(Wallpaper.uploader),
            selectinload(Wallpaper.category),
        )
    )
    if not wp:
        raise HTTPException(404, "Wallpaper não encontrado.")
    is_owner = user is not None and user.id == wp.uploader_id
    if wp.status != "approved" and not (is_owner or (user and user.is_staff)):
        raise HTTPException(404, "Wallpaper não encontrado.")
    if wp.purity not in allowed_purities(user):
        raise HTTPException(401, "Entre na sua conta para ver conteúdo explícito.")
    return wp


@router.get("/w/{wid}")
def wallpaper_page(wid: int, request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    wp = load_wallpaper(db, wid, user)
    seen = request.session.setdefault("seen", [])
    if wp.status == "approved" and wid not in seen:
        wp.views += 1
        db.commit()
        request.session["seen"] = (seen + [wid])[-200:]
    favorited = bool(
        user and db.scalar(select(Favorite).where(Favorite.user_id == user.id, Favorite.wallpaper_id == wid))
    )
    similar_params = SearchParams(q=f"like:{wid}", purity=allowed_purities(user), sorting="relevance")
    similar, _, _ = run_search(db, similar_params, 8)
    return render(
        request,
        "wallpaper.html",
        {
            "wp": wp,
            "favorited": favorited,
            "similar": similar,
            "can_edit": bool(user and (user.id == wp.uploader_id or user.is_staff)),
            "categories": db.scalars(select(Category).order_by(Category.sort_order)).all(),
        },
    )


# ------------------------------------------------------------------ perfis


@router.get("/u/{username}")
def profile(username: str, request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    owner = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if not owner:
        raise HTTPException(404, "Usuário não encontrado.")
    params = params_from_request(request, user, q=f"@{owner.username} " + request.query_params.get("q", ""))
    ctx = search_context(db, request, user, params, profile=owner, tab="uploads", title=owner.username)
    ctx["pending"] = []
    if user and (user.id == owner.id or user.is_staff):
        ctx["pending"] = db.scalars(
            select(Wallpaper)
            .where(Wallpaper.uploader_id == owner.id, Wallpaper.status != "approved")
            .order_by(Wallpaper.created_at.desc())
            .limit(48)
        ).all()
    ctx["fav_count"] = db.scalar(select(func.count()).select_from(Favorite).where(Favorite.user_id == owner.id))
    return render(request, "profile.html", ctx)


@router.get("/u/{username}/favorites")
def profile_favorites(username: str, request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    owner = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if not owner:
        raise HTTPException(404, "Usuário não encontrado.")
    params = params_from_request(request, user, favorites_of=owner.id)
    ctx = search_context(db, request, user, params, profile=owner, tab="favorites", title=f"Favoritos de {owner.username}")
    ctx["pending"] = []
    ctx["fav_count"] = ctx["total"]
    return render(request, "profile.html", ctx)


# ------------------------------------------------------------------ categorias e cores


def category_color_stats(db: Session, category_id: int, limit: int = 10) -> list[tuple[str, float]]:
    """Cores mais presentes nos wallpapers aprovados de uma categoria (média ponderada)."""
    total = db.scalar(
        select(func.count()).select_from(Wallpaper).where(Wallpaper.category_id == category_id, Wallpaper.status == "approved")
    )
    if not total:
        return []
    rows = db.execute(
        select(WallpaperColor.hex, func.sum(WallpaperColor.percent))
        .join(Wallpaper)
        .where(Wallpaper.category_id == category_id, Wallpaper.status == "approved")
        .group_by(WallpaperColor.hex)
        .order_by(func.sum(WallpaperColor.percent).desc())
        .limit(limit)
    ).all()
    return [(hx, round(s / total, 1)) for hx, s in rows]


@router.get("/categories")
def categories(request: Request, db: Session = Depends(get_db)):
    cats = db.scalars(select(Category).options(selectinload(Category.colors)).order_by(Category.sort_order)).all()
    counts = dict(
        db.execute(
            select(Wallpaper.category_id, func.count()).where(Wallpaper.status == "approved").group_by(Wallpaper.category_id)
        ).all()
    )
    stats = {c.id: category_color_stats(db, c.id, 6) for c in cats}
    return render(request, "categories.html", {"cats": cats, "counts": counts, "stats": stats})


@router.get("/c/{slug}")
def category_page(slug: str, request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    cat = db.scalar(select(Category).where(Category.slug == slug).options(selectinload(Category.colors)))
    if not cat:
        raise HTTPException(404, "Categoria não encontrada.")
    params = params_from_request(request, user, categories=[cat.id])
    ctx = search_context(db, request, user, params, category=cat, color_stats=category_color_stats(db, cat.id), title=cat.name)
    return render(request, "category.html", ctx)


@router.get("/colors")
def colors_page(request: Request, db: Session = Depends(get_db)):
    counts = dict(
        db.execute(
            select(WallpaperColor.hex, func.count(func.distinct(WallpaperColor.wallpaper_id)))
            .join(Wallpaper)
            .where(Wallpaper.status == "approved", WallpaperColor.percent >= 5)
            .group_by(WallpaperColor.hex)
        ).all()
    )
    cats = db.scalars(select(Category).options(selectinload(Category.colors)).order_by(Category.sort_order)).all()
    return render(request, "colors.html", {"counts": counts, "cats": cats})


# ------------------------------------------------------------------ tags e personagens


@router.get("/tags")
def tags_index(request: Request, q: str = "", db: Session = Depends(get_db)):
    roots = db.scalars(
        select(TagCategory).where(TagCategory.parent_id.is_(None)).options(selectinload(TagCategory.children))
    ).all()
    stmt = select(Tag).options(selectinload(Tag.tag_category).selectinload(TagCategory.parent))
    if q:
        stmt = stmt.where(Tag.name.ilike(f"%{q.strip()}%"))
    tags = db.scalars(stmt.order_by(Tag.usage_count.desc(), Tag.name).limit(600)).all()
    by_cat: dict[int | None, list[Tag]] = {}
    for t in tags:
        by_cat.setdefault(t.tag_category_id, []).append(t)
    return render(request, "tags.html", {"roots": roots, "by_cat": by_cat, "q": q, "tags": tags})


@router.get("/tag/{slug}")
def tag_page(slug: str, request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    tag = db.scalar(
        select(Tag)
        .where(Tag.slug == slug)
        .options(selectinload(Tag.aliases), selectinload(Tag.implies), selectinload(Tag.tag_category))
    )
    if not tag:
        raise HTTPException(404, "Tag não encontrada.")
    implied_by = db.scalars(select(Tag).where(Tag.implies.any(Tag.id == tag.id))).all()
    related = db.execute(
        select(Tag, func.count())
        .join(WallpaperTag, WallpaperTag.tag_id == Tag.id)
        .where(
            WallpaperTag.wallpaper_id.in_(select(WallpaperTag.wallpaper_id).where(WallpaperTag.tag_id == tag.id)),
            Tag.id != tag.id,
        )
        .group_by(Tag.id)
        .order_by(func.count().desc())
        .limit(20)
    ).all()
    extra = request.query_params.get("q", "")
    params = params_from_request(request, user, q=f'tag:{tag.slug} {extra}'.strip())
    ctx = search_context(db, request, user, params, tag=tag, implied_by=implied_by, related=related, title=tag.name)
    return render(request, "tag.html", ctx)


@router.get("/characters")
def characters_index(request: Request, q: str = "", db: Session = Depends(get_db)):
    stmt = select(Character).options(selectinload(Character.series))
    if q:
        stmt = stmt.where(Character.name.ilike(f"%{q.strip()}%") | Character.aliases.ilike(f"%{q.strip()}%"))
    chars = db.scalars(stmt.order_by(Character.usage_count.desc(), Character.name).limit(300)).all()
    series = db.scalars(select(Series).order_by(Series.name)).all()
    return render(request, "characters.html", {"chars": chars, "series": series, "q": q})


@router.get("/character/{slug}")
def character_page(slug: str, request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    ch = db.scalar(select(Character).where(Character.slug == slug).options(selectinload(Character.series)))
    if not ch:
        raise HTTPException(404, "Personagem não encontrado.")
    params = params_from_request(request, user, q=f"char:{ch.slug} " + request.query_params.get("q", ""))
    return render(request, "character.html", search_context(db, request, user, params, character=ch, title=ch.name))


@router.get("/series/{slug}")
def series_page(slug: str, request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    s = db.scalar(select(Series).where(Series.slug == slug).options(selectinload(Series.characters)))
    if not s:
        raise HTTPException(404, "Série não encontrada.")
    params = params_from_request(request, user, q=f"series:{s.slug} " + request.query_params.get("q", ""))
    return render(request, "series.html", search_context(db, request, user, params, series=s, title=s.name))
