from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..db import get_db
from ..models import Character, Tag, TagAlias
from ..security import current_user
from ..utils import clamp_int
from .browse import params_from_request, run_search

router = APIRouter(prefix="/api")


@router.get("/tags/autocomplete")
def tag_autocomplete(q: str = "", db: Session = Depends(get_db)):
    q = q.strip().lower()
    if len(q) < 1:
        return []
    tags = db.scalars(
        select(Tag)
        .where(or_(Tag.name.ilike(f"{q}%"), Tag.name.ilike(f"% {q}%")))
        .options(selectinload(Tag.tag_category))
        .order_by(Tag.usage_count.desc(), Tag.name)
        .limit(10)
    ).all()
    out = [
        {"name": t.name, "slug": t.slug, "count": t.usage_count, "purity": t.purity,
         "category": t.tag_category.name if t.tag_category else None}
        for t in tags
    ]
    seen = {t.id for t in tags}
    for alias in db.scalars(select(TagAlias).where(TagAlias.alias.ilike(f"{q}%")).limit(5)):
        if alias.tag_id not in seen:
            seen.add(alias.tag_id)
            out.append({"name": alias.tag.name, "slug": alias.tag.slug, "count": alias.tag.usage_count,
                        "purity": alias.tag.purity, "alias": alias.alias})
    return out


@router.get("/characters/autocomplete")
def char_autocomplete(q: str = "", db: Session = Depends(get_db)):
    q = q.strip()
    if not q:
        return []
    chars = db.scalars(
        select(Character)
        .where(or_(Character.name.ilike(f"%{q}%"), Character.aliases.ilike(f"%{q}%")))
        .options(selectinload(Character.series))
        .order_by(Character.usage_count.desc())
        .limit(10)
    ).all()
    return [
        {"name": c.name, "slug": c.slug, "series": c.series.name if c.series else None, "count": c.usage_count,
         "label": f"{c.name} ({c.series.name})" if c.series else c.name}
        for c in chars
    ]


@router.get("/search")
def api_search(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    """Mesmos parâmetros da página /search, em JSON (útil para apps e scroll infinito)."""
    params = params_from_request(request, user)
    per_page = clamp_int(request.query_params.get("per_page"), get_settings().per_page, 1, 64)
    items, total, pq = run_search(db, params, per_page)
    return {
        "meta": {"page": params.page, "per_page": per_page, "total": total, "unknown_terms": pq.unknown,
                 "seed": params.seed},
        "data": [
            {
                "id": w.id, "url": f"/w/{w.id}", "thumb": f"/media/{w.id}/thumb", "full": f"/media/{w.id}/full",
                "resolution": w.resolution, "ratio": w.ratio_label, "purity": w.purity,
                "views": w.views, "favorites": w.favorites_count, "file_size": w.file_size,
                "colors": [c.hex for c in w.colors], "created_at": w.created_at.isoformat(),
            }
            for w in items
        ],
    }
