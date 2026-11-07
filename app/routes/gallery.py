"""Galeria privada: o usuário salva wallpapers do site em pastas (e subpastas) só dele."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..models import Collection, CollectionItem, User, Wallpaper
from ..security import csrf_protect, require_user
from ..web import flash, render
from .browse import allowed_purities

router = APIRouter(prefix="/gallery")

MAX_FOLDERS = 500


def _own(db: Session, cid: int, user: User) -> Collection:
    col = db.get(Collection, cid)
    if not col or col.user_id != user.id:  # 404 também para pastas de outros (não revela existência)
        raise HTTPException(404, "Pasta não encontrada.")
    return col


def user_folders(db: Session, user: User) -> list[Collection]:
    return db.scalars(select(Collection).where(Collection.user_id == user.id).order_by(Collection.name)).all()


def folder_tree(folders: list[Collection]) -> list[tuple[Collection, int]]:
    """Lista em ordem de árvore com profundidade, para selects e a barra lateral."""
    by_parent: dict[int | None, list[Collection]] = {}
    for f in folders:
        by_parent.setdefault(f.parent_id, []).append(f)
    out: list[tuple[Collection, int]] = []

    def walk(pid, depth):
        for f in by_parent.get(pid, []):
            out.append((f, depth))
            walk(f.id, depth + 1)

    walk(None, 0)
    return out


@router.get("")
def gallery_home(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return _render_folder(request, user, db, None)


@router.get("/{cid}")
def gallery_folder(cid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return _render_folder(request, user, db, _own(db, cid, user))


def _render_folder(request: Request, user: User, db: Session, folder: Collection | None):
    folders = user_folders(db, user)
    counts = dict(
        db.execute(
            select(CollectionItem.collection_id, func.count())
            .join(Collection)
            .where(Collection.user_id == user.id)
            .group_by(CollectionItem.collection_id)
        ).all()
    )
    items: list[Wallpaper] = []
    if folder:
        items = db.scalars(
            select(Wallpaper)
            .join(CollectionItem, CollectionItem.wallpaper_id == Wallpaper.id)
            .where(
                CollectionItem.collection_id == folder.id,
                Wallpaper.status == "approved",
                Wallpaper.purity.in_(allowed_purities(user)),
            )
            .options(selectinload(Wallpaper.colors))
            .order_by(CollectionItem.created_at.desc())
        ).all()
    crumbs, node = [], folder
    while node is not None:
        crumbs.insert(0, node)
        node = node.parent
    return render(
        request,
        "gallery.html",
        {
            "folder": folder,
            "tree": folder_tree(folders),
            "children": [f for f in folders if f.parent_id == (folder.id if folder else None)],
            "counts": counts,
            "items": items,
            "crumbs": crumbs,
        },
    )


@router.post("/folders", dependencies=[Depends(csrf_protect)])
async def create_folder(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    form = await request.form()
    name = str(form.get("name", "")).strip()[:64]
    parent = str(form.get("parent", ""))
    parent_obj = _own(db, int(parent), user) if parent.isdigit() else None
    if not name:
        flash(request, "Dê um nome à pasta.", "error")
    elif db.scalar(select(func.count()).select_from(Collection).where(Collection.user_id == user.id)) >= MAX_FOLDERS:
        flash(request, "Limite de pastas atingido.", "error")
    else:
        try:
            db.add(Collection(user_id=user.id, name=name, parent=parent_obj))
            db.commit()
        except IntegrityError:
            db.rollback()
            flash(request, "Você já tem uma pasta com esse nome.", "error")
    return RedirectResponse(f"/gallery/{parent_obj.id}" if parent_obj else "/gallery", status_code=303)


@router.post("/{cid}/edit", dependencies=[Depends(csrf_protect)])
async def edit_folder(cid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    col = _own(db, cid, user)
    form = await request.form()
    if form.get("action") == "delete":
        parent_id = col.parent_id
        db.delete(col)
        db.commit()
        flash(request, "Pasta apagada (os wallpapers continuam no site).", "ok")
        return RedirectResponse(f"/gallery/{parent_id}" if parent_id else "/gallery", status_code=303)
    name = str(form.get("name", "")).strip()[:64]
    if name:
        col.name = name
    col.description = str(form.get("description", ""))[:500]
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, "Já existe uma pasta com esse nome.", "error")
    return RedirectResponse(f"/gallery/{cid}", status_code=303)


@router.post("/save", dependencies=[Depends(csrf_protect)])
async def save_to_folder(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Salva/remove um wallpaper numa pasta. Aceita new_folder para criar e salvar de uma vez."""
    form = await request.form()
    wid = str(form.get("wallpaper_id", ""))
    wp = db.get(Wallpaper, int(wid)) if wid.isdigit() else None
    if not wp or wp.status != "approved":
        raise HTTPException(404, "Wallpaper não encontrado.")
    new_name = str(form.get("new_folder", "")).strip()[:64]
    if new_name:
        col = db.scalar(select(Collection).where(Collection.user_id == user.id, Collection.name == new_name))
        if not col:
            col = Collection(user_id=user.id, name=new_name)
            db.add(col)
            db.flush()
    else:
        cid = str(form.get("collection_id", ""))
        if not cid.isdigit():
            raise HTTPException(400, "Escolha uma pasta.")
        col = _own(db, int(cid), user)
    item = db.get(CollectionItem, (col.id, wp.id))
    if item and form.get("action") == "remove":
        db.delete(item)
        saved = False
    elif item:
        saved = True
    else:
        db.add(CollectionItem(collection_id=col.id, wallpaper_id=wp.id))
        saved = True
    db.commit()
    if "application/json" in request.headers.get("accept", ""):
        return {"saved": saved, "folder": {"id": col.id, "name": col.name}}
    flash(request, f"{'Salvo em' if saved else 'Removido de'} '{col.name}'.", "ok")
    return RedirectResponse(str(form.get("back") or f"/w/{wp.id}"), status_code=303)


@router.get("/api/folders-for/{wid}")
def folders_for(wid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    folders = user_folders(db, user)
    inside = set(
        db.scalars(
            select(CollectionItem.collection_id).where(
                CollectionItem.wallpaper_id == wid, CollectionItem.collection_id.in_([f.id for f in folders])
            )
        )
    )
    return [{"id": f.id, "name": f.name, "depth": d, "saved": f.id in inside} for f, d in folder_tree(folders)]
