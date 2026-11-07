from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..imaging import PALETTE
from ..models import (
    PURITIES,
    ROLES,
    Category,
    CategoryColor,
    Report,
    Tag,
    TagAlias,
    TagCategory,
    User,
    Wallpaper,
    WallpaperTag,
    utcnow,
)
from ..security import csrf_protect, require_staff
from ..services import add_implication, refresh_all_counts, refresh_tag_counts, resolve_tag, split_list
from ..utils import normalize_tag_name, slugify
from ..web import flash, render

router = APIRouter(prefix="/admin")


@router.get("")
def queue(request: Request, staff: User = Depends(require_staff), db: Session = Depends(get_db)):
    pending = db.scalars(
        select(Wallpaper)
        .where(Wallpaper.status == "pending")
        .options(selectinload(Wallpaper.uploader), selectinload(Wallpaper.tag_links).selectinload(WallpaperTag.tag))
        .order_by(Wallpaper.created_at)
        .limit(60)
    ).all()
    open_reports = db.scalar(select(func.count()).select_from(Report).where(Report.status == "open"))
    return render(request, "admin/queue.html", {"pending": pending, "open_reports": open_reports})


@router.post("/review/{wid}", dependencies=[Depends(csrf_protect)])
async def review(wid: int, request: Request, staff: User = Depends(require_staff), db: Session = Depends(get_db)):
    form = await request.form()
    wp = db.get(Wallpaper, wid)
    if not wp:
        raise HTTPException(404, "Wallpaper não encontrado.")
    decision = form.get("decision")
    if decision in ("approved", "rejected"):
        wp.status = decision
        wp.reviewed_at = utcnow()
        wp.reviewed_by_id = staff.id
        if form.get("purity") in PURITIES:
            wp.purity = form.get("purity")
        refresh_all_counts(db, wp)
        db.commit()
    return RedirectResponse(form.get("back") or "/admin", status_code=303)


# ------------------------------------------------------------------ denúncias


@router.get("/reports")
def reports(request: Request, staff: User = Depends(require_staff), db: Session = Depends(get_db)):
    from .wallpapers import REPORT_REASONS

    items = db.scalars(
        select(Report)
        .where(Report.status == "open")
        .options(selectinload(Report.wallpaper), selectinload(Report.user))
        .order_by((Report.reason == "underage").desc(), Report.created_at)
    ).all()
    return render(request, "admin/reports.html", {"reports": items, "reasons": REPORT_REASONS})


@router.post("/reports/{rid}", dependencies=[Depends(csrf_protect)])
async def resolve_report(rid: int, request: Request, staff: User = Depends(require_staff), db: Session = Depends(get_db)):
    form = await request.form()
    rep = db.get(Report, rid)
    if rep:
        action = form.get("action")
        rep.status = "resolved" if action in ("resolved", "remove") else "dismissed"
        if action == "remove" and rep.wallpaper:
            rep.wallpaper.status = "rejected"
            refresh_all_counts(db, rep.wallpaper)
        elif action == "dismissed" and rep.wallpaper and rep.wallpaper.status == "pending":
            rep.wallpaper.status = "approved"
            refresh_all_counts(db, rep.wallpaper)
        db.commit()
    return RedirectResponse("/admin/reports", status_code=303)


# ------------------------------------------------------------------ tags


@router.get("/tags/{slug}")
def tag_edit_page(slug: str, request: Request, staff: User = Depends(require_staff), db: Session = Depends(get_db)):
    tag = db.scalar(select(Tag).where(Tag.slug == slug).options(selectinload(Tag.aliases), selectinload(Tag.implies)))
    if not tag:
        raise HTTPException(404, "Tag não encontrada.")
    tcats = db.scalars(select(TagCategory).options(selectinload(TagCategory.parent))).all()
    return render(request, "admin/tag_edit.html", {"tag": tag, "tcats": sorted(tcats, key=lambda c: c.path)})


@router.post("/tags/{slug}", dependencies=[Depends(csrf_protect)])
async def tag_edit(slug: str, request: Request, staff: User = Depends(require_staff), db: Session = Depends(get_db)):
    tag = db.scalar(select(Tag).where(Tag.slug == slug))
    if not tag:
        raise HTTPException(404, "Tag não encontrada.")
    form = await request.form()
    action = form.get("action")

    if action == "save":
        new_name = normalize_tag_name(str(form.get("name", tag.name)))
        if new_name and new_name != tag.name and not resolve_tag(db, new_name):
            tag.name = new_name
        tag.description = str(form.get("description", ""))[:2000]
        if form.get("purity") in PURITIES:
            tag.purity = form.get("purity")
        tc = str(form.get("tag_category", ""))
        tag.tag_category_id = int(tc) if tc.isdigit() else None
        # aliases
        wanted = {normalize_tag_name(a) for a in split_list(str(form.get("aliases", "")))}
        wanted.discard(tag.name)
        for a in list(tag.aliases):
            if a.alias not in wanted:
                tag.aliases.remove(a)
        have = {a.alias for a in tag.aliases}
        for a in wanted - have:
            other = resolve_tag(db, a)
            if other is None:
                tag.aliases.append(TagAlias(alias=a))
            elif other.id != tag.id:
                flash(request, f"'{a}' já é outra tag; use 'mesclar'.", "error")
        # implicações
        tag.implies.clear()
        db.flush()
        for name in split_list(str(form.get("implies", ""))):
            imp = resolve_tag(db, name)
            if imp and (err := add_implication(db, tag, imp)):
                flash(request, err, "error")
        db.commit()
        flash(request, "Tag salva. Implicações novas valem para edições futuras.", "ok")

    elif action == "merge":
        target = resolve_tag(db, str(form.get("merge_into", "")))
        if not target or target.id == tag.id:
            flash(request, "Tag de destino inválida.", "error")
        else:
            for link in db.scalars(select(WallpaperTag).where(WallpaperTag.tag_id == tag.id)).all():
                if db.get(WallpaperTag, (link.wallpaper_id, target.id)):
                    db.delete(link)
                else:
                    link.tag_id = target.id
            old_name, old_aliases = tag.name, [a.alias for a in tag.aliases]
            db.delete(tag)
            db.flush()
            for a in [old_name, *old_aliases]:
                if not resolve_tag(db, a):
                    db.add(TagAlias(tag_id=target.id, alias=a))
            refresh_tag_counts(db, [target.id])
            db.commit()
            flash(request, f"'{old_name}' mesclada em '{target.name}' (virou alias).", "ok")
            return RedirectResponse(f"/tag/{target.slug}", status_code=303)

    elif action == "delete":
        db.delete(tag)
        db.commit()
        flash(request, "Tag apagada.", "ok")
        return RedirectResponse("/tags", status_code=303)
    return RedirectResponse(f"/admin/tags/{tag.slug}", status_code=303)


@router.post("/tag-categories", dependencies=[Depends(csrf_protect)])
async def add_tag_category(request: Request, staff: User = Depends(require_staff), db: Session = Depends(get_db)):
    form = await request.form()
    name = str(form.get("name", "")).strip()[:64]
    parent = str(form.get("parent", ""))
    if name:
        parent_obj = db.get(TagCategory, int(parent)) if parent.isdigit() else None
        slug = slugify(f"{parent_obj.name}-{name}" if parent_obj else name)
        if not db.scalar(select(TagCategory).where(TagCategory.slug == slug)):
            db.add(TagCategory(name=name, slug=slug, parent=parent_obj))
            db.commit()
    return RedirectResponse("/tags", status_code=303)


# ------------------------------------------------------------------ categorias e cores


@router.get("/categories")
def categories_page(request: Request, staff: User = Depends(require_staff), db: Session = Depends(get_db)):
    cats = db.scalars(select(Category).options(selectinload(Category.colors)).order_by(Category.sort_order)).all()
    return render(request, "admin/categories.html", {"cats": cats, "palette": PALETTE})


@router.post("/categories", dependencies=[Depends(csrf_protect)])
async def categories_save(request: Request, staff: User = Depends(require_staff), db: Session = Depends(get_db)):
    form = await request.form()
    cid = str(form.get("id", ""))
    name = str(form.get("name", "")).strip()[:64]
    if form.get("action") == "delete" and cid.isdigit():
        cat = db.get(Category, int(cid))
        if cat:
            db.delete(cat)
            db.commit()
        return RedirectResponse("/admin/categories", status_code=303)
    if not name:
        flash(request, "Nome obrigatório.", "error")
        return RedirectResponse("/admin/categories", status_code=303)
    cat = db.get(Category, int(cid)) if cid.isdigit() else None
    if cat is None:
        cat = Category(name=name, slug=slugify(name), sort_order=99)
        db.add(cat)
    cat.name = name
    cat.description = str(form.get("description", ""))[:1000]
    so = str(form.get("sort_order", ""))
    if so.lstrip("-").isdigit():
        cat.sort_order = int(so)
    colors = [c for c in form.getlist("colors") if c in PALETTE]
    cat.colors.clear()
    db.flush()
    cat.colors.extend(CategoryColor(hex=h, label=PALETTE[h], position=i) for i, h in enumerate(colors[:8]))
    db.commit()
    flash(request, f"Categoria '{cat.name}' salva.", "ok")
    return RedirectResponse("/admin/categories", status_code=303)


# ------------------------------------------------------------------ usuários


@router.get("/users")
def users_page(request: Request, q: str = "", staff: User = Depends(require_staff), db: Session = Depends(get_db)):
    stmt = select(User).order_by(User.created_at.desc()).limit(100)
    if q:
        stmt = stmt.where(User.username.ilike(f"%{q}%") | User.email.ilike(f"%{q}%"))
    return render(request, "admin/users.html", {"users": db.scalars(stmt).all(), "q": q, "roles": ROLES})


@router.post("/users/{uid}", dependencies=[Depends(csrf_protect)])
async def users_update(uid: int, request: Request, staff: User = Depends(require_staff), db: Session = Depends(get_db)):
    form = await request.form()
    target = db.get(User, uid)
    if not target or target.id == staff.id:
        flash(request, "Você não pode alterar a própria conta aqui.", "error")
        return RedirectResponse("/admin/users", status_code=303)
    if target.role == "admin" and staff.role != "admin":
        raise HTTPException(403, "Só administradores alteram administradores.")
    if form.get("role") in ROLES and staff.role == "admin":
        target.role = form.get("role")
    target.is_banned = form.get("banned") == "yes"
    db.commit()
    flash(request, f"{target.username} atualizado.", "ok")
    return RedirectResponse("/admin/users", status_code=303)
