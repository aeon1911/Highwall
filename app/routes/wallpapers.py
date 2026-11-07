from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.datastructures import UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import PURITIES, Category, Favorite, Report, User, Wallpaper
from ..security import client_ip, csrf_protect, require_user, upload_limiter
from ..services import (
    UploadError,
    create_wallpaper,
    refresh_all_counts,
    refresh_character_counts,
    refresh_tag_counts,
    set_wallpaper_characters,
    set_wallpaper_tags,
    split_list,
)
from ..storage import StorageError, get_storage
from ..web import flash, render

router = APIRouter()

REPORT_REASONS = {
    "underage": "Pessoa aparenta ser menor de idade",
    "non_consensual": "Conteúdo não consensual / vazado",
    "copyright": "Violação de direitos autorais",
    "wrong_purity": "Classificação de pureza errada",
    "wrong_tags": "Tags ou categoria erradas",
    "low_quality": "Baixa qualidade / upscale",
    "other": "Outro",
}


@router.get("/upload")
def upload_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    s = get_settings()
    return render(
        request,
        "upload.html",
        {
            "categories": db.scalars(select(Category).order_by(Category.sort_order)).all(),
            "min_long": s.min_long_side,
            "min_short": s.min_short_side,
            "max_mb": s.max_upload_mb,
        },
    )


@router.post("/upload", dependencies=[Depends(csrf_protect)])
async def upload_submit(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Recebe um arquivo por requisição (o front envia vários em sequência) e responde em JSON."""
    s = get_settings()
    length = int(request.headers.get("content-length") or 0)
    if length > s.max_upload_bytes + 1024 * 1024:
        return JSONResponse({"ok": False, "error": f"Arquivo maior que {s.max_upload_mb} MB."}, 413)
    if not upload_limiter.hit(f"u:{user.id}"):
        return JSONResponse({"ok": False, "error": "Limite de uploads por hora atingido."}, 429)

    form = await request.form()
    file = form.get("file")
    if not isinstance(file, UploadFile) or not file.filename:
        return JSONResponse({"ok": False, "error": "Nenhum arquivo enviado."}, 400)
    data = await file.read(s.max_upload_bytes + 1)
    if form.get("confirm_adults") != "yes":
        return JSONResponse(
            {"ok": False, "error": "Confirme que todas as pessoas retratadas são maiores de 18 anos."}, 400
        )

    cat = form.get("category")
    try:
        wp = await create_wallpaper(
            db,
            get_storage(),
            user,
            data,
            category_id=int(cat) if cat and str(cat).isdigit() else None,
            purity=str(form.get("purity", "sketchy")),
            title=str(form.get("title", "")),
            source=str(form.get("source", "")),
            tags=split_list(str(form.get("tags", ""))),
            characters=split_list(str(form.get("characters", ""))),
        )
    except UploadError as e:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(e)}, 422)
    except StorageError as e:
        db.rollback()
        return JSONResponse({"ok": False, "error": f"Falha ao armazenar: {e}"}, 502)
    except IntegrityError:
        db.rollback()
        return JSONResponse({"ok": False, "error": "Este wallpaper já existe no site."}, 409)
    return JSONResponse(
        {"ok": True, "id": wp.id, "url": f"/w/{wp.id}", "status": wp.status, "resolution": wp.resolution}
    )


def _get_editable(db: Session, wid: int, user: User) -> Wallpaper:
    wp = db.get(Wallpaper, wid)
    if not wp:
        raise HTTPException(404, "Wallpaper não encontrado.")
    if wp.uploader_id != user.id and not user.is_staff:
        raise HTTPException(403, "Você não pode editar este wallpaper.")
    return wp


@router.post("/w/{wid}/edit", dependencies=[Depends(csrf_protect)])
async def edit_wallpaper(wid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    wp = _get_editable(db, wid, user)
    form = await request.form()
    wp.title = str(form.get("title", wp.title))[:140]
    wp.source = str(form.get("source", wp.source))[:500]
    purity = str(form.get("purity", wp.purity))
    if purity in PURITIES:
        wp.purity = purity
    cat = str(form.get("category", ""))
    wp.category_id = int(cat) if cat.isdigit() and db.get(Category, int(cat)) else None
    # tags marcadas como implícitas são recalculadas, então só enviamos as explícitas
    set_wallpaper_tags(db, wp, split_list(str(form.get("tags", ""))), user)
    set_wallpaper_characters(db, wp, split_list(str(form.get("characters", ""))))
    db.commit()
    flash(request, "Wallpaper atualizado.", "ok")
    return RedirectResponse(f"/w/{wid}", status_code=303)


@router.post("/w/{wid}/favorite", dependencies=[Depends(csrf_protect)])
def toggle_favorite(wid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    wp = db.get(Wallpaper, wid)
    if not wp or wp.status != "approved":
        raise HTTPException(404, "Wallpaper não encontrado.")
    fav = db.get(Favorite, (user.id, wid))
    if fav:
        db.delete(fav)
        wp.favorites_count = max(0, wp.favorites_count - 1)
        state = False
    else:
        db.add(Favorite(user_id=user.id, wallpaper_id=wid))
        wp.favorites_count += 1
        state = True
    db.commit()
    return {"favorited": state, "count": wp.favorites_count}


@router.post("/w/{wid}/report", dependencies=[Depends(csrf_protect)])
async def report(wid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    form = await request.form()
    reason = str(form.get("reason", "other"))
    if reason not in REPORT_REASONS:
        reason = "other"
    wp = db.get(Wallpaper, wid)
    if not wp:
        raise HTTPException(404, "Wallpaper não encontrado.")
    if not db.scalar(select(Report).where(Report.wallpaper_id == wid, Report.user_id == user.id)):
        db.add(Report(wallpaper_id=wid, user_id=user.id, reason=reason, details=str(form.get("details", ""))[:2000]))
        # denúncia de possível menor de idade tira o wallpaper do ar imediatamente até revisão
        if reason == "underage" and wp.status == "approved":
            wp.status = "pending"
            refresh_all_counts(db, wp)
        db.commit()
    flash(request, "Denúncia enviada. Obrigado!", "ok")
    return RedirectResponse(f"/w/{wid}" if wp.status == "approved" else "/", status_code=303)


@router.post("/w/{wid}/delete", dependencies=[Depends(csrf_protect)])
async def delete_wallpaper(wid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    wp = _get_editable(db, wid, user)
    storage = get_storage()
    for ref, mid in ((wp.file_ref, wp.file_message_id), (wp.thumb_ref, wp.thumb_message_id)):
        try:
            await storage.delete(ref, mid)
        except StorageError:
            pass
    tag_ids = [link.tag_id for link in wp.tag_links]
    char_ids = [link.character_id for link in wp.character_links]
    db.delete(wp)
    db.flush()
    refresh_tag_counts(db, tag_ids)
    refresh_character_counts(db, char_ids)
    db.commit()
    flash(request, f"Wallpaper #{wid} removido.", "ok")
    return RedirectResponse(f"/u/{user.username}", status_code=303)


@router.get("/report-reasons")
def report_reasons():
    return REPORT_REASONS
