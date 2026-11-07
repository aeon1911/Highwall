from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User, Wallpaper
from ..security import current_user
from ..storage import StorageError, fetch_cached
from .browse import allowed_purities

router = APIRouter(prefix="/media")


def _check_access(db: Session, wid: int, user: User | None) -> Wallpaper:
    wp = db.get(Wallpaper, wid)
    if not wp:
        raise HTTPException(404, "not_found")
    if wp.status != "approved" and not (user and (user.id == wp.uploader_id or user.is_staff)):
        raise HTTPException(404, "not_found")
    if wp.purity not in allowed_purities(user):
        raise HTTPException(401, "login_required")
    return wp


async def _serve(ref: str, media_type: str, private: bool, filename: str | None = None):
    try:
        path = await fetch_cached(ref)
    except StorageError as e:
        raise HTTPException(502, f"storage_error: {e}") from None
    headers = {"Cache-Control": f"{'private' if private else 'public'}, max-age=604800, immutable"}
    if filename:
        return FileResponse(path, media_type=media_type, headers=headers, filename=filename)
    return FileResponse(path, media_type=media_type, headers=headers)


@router.get("/{wid}/thumb")
async def thumb(wid: int, user=Depends(current_user), db: Session = Depends(get_db)):
    wp = _check_access(db, wid, user)
    return await _serve(wp.thumb_ref, "image/webp", private=wp.purity == "nsfw" or wp.status != "approved")


@router.get("/{wid}/full")
async def full(wid: int, request: Request, download: bool = False, user=Depends(current_user), db: Session = Depends(get_db)):
    wp = _check_access(db, wid, user)
    ext = wp.mime.split("/")[-1].replace("jpeg", "jpg")
    name = f"high-{wp.id}-{wp.width}x{wp.height}.{ext}" if download else None
    return await _serve(wp.file_ref, wp.mime, private=True, filename=name)
