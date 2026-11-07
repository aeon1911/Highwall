"""Vault pessoal com criptografia de ponta a ponta (client-side).

O servidor é "cego": recebe e devolve apenas bytes cifrados. Toda a criptografia
acontece em app/static/js/vault.js usando a WebCrypto API do navegador:

  senha do vault ──PBKDF2-SHA256 (600k it., salt)──▶ KEK (chave de embrulho)
  chave-mestra (AES-256, aleatória) ──AES-GCM com KEK──▶ wrapped_key (vai p/ servidor)
  cada arquivo ──AES-GCM com a chave-mestra──▶ blob cifrado (vai p/ Telegram)

O que o servidor guarda: salt, nº de iterações, wrapped_key, blobs e metadados cifrados.
O que ele NUNCA recebe: a senha do vault, a KEK ou a chave-mestra.
Consequência: se o dono esquecer a senha e perder a chave de recuperação, os arquivos
são irrecuperáveis — nem a administração do site consegue abrir.
"""

from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.datastructures import UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import User, VaultFolder, VaultItem, VaultKey, utcnow
from ..security import csrf_protect, require_user
from ..storage import StorageError, fetch_cached, get_storage
from ..web import render

router = APIRouter()

MIN_ITERATIONS = 300_000
VAULT_QUOTA_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB por usuário
MAX_META_LEN = 8192


def _b64(value: str, max_len: int, field: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > max_len:
        raise HTTPException(400, f"{field} inválido")
    try:
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, f"{field} inválido") from None
    return value


@router.get("/vault")
def vault_page(request: Request, user: User = Depends(require_user)):
    return render(request, "vault.html", {"max_mb": get_settings().max_upload_mb})


# ------------------------------------------------------------------ chaves


@router.get("/api/vault/key")
def get_key(user: User = Depends(require_user), db: Session = Depends(get_db)):
    vk = db.get(VaultKey, user.id)
    if not vk:
        return {"exists": False}
    used = db.scalar(select(func.coalesce(func.sum(VaultItem.size), 0)).where(VaultItem.user_id == user.id))
    return {
        "exists": True,
        "kdf": vk.kdf,
        "iterations": vk.kdf_iterations,
        "salt": vk.salt,
        "wrapped_key": vk.wrapped_key,
        "recovery_wrapped_key": vk.recovery_wrapped_key,
        "used_bytes": used,
        "quota_bytes": VAULT_QUOTA_BYTES,
    }


@router.post("/api/vault/key", dependencies=[Depends(csrf_protect)])
async def set_key(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Cria o vault ou troca a senha (re-embrulha a MESMA chave-mestra; arquivos não mudam)."""
    body = await request.json()
    iterations = int(body.get("iterations", 0))
    if iterations < MIN_ITERATIONS or iterations > 10_000_000:
        raise HTTPException(400, "Iterações fora do intervalo permitido")
    salt = _b64(body.get("salt"), 64, "salt")
    wrapped = _b64(body.get("wrapped_key"), 255, "wrapped_key")
    recovery = body.get("recovery_wrapped_key")
    vk = db.get(VaultKey, user.id)
    if vk is None:
        if not recovery:
            raise HTTPException(400, "Chave de recuperação obrigatória na criação")
        vk = VaultKey(user_id=user.id, kdf_iterations=iterations, salt=salt, wrapped_key=wrapped,
                      recovery_wrapped_key=_b64(recovery, 255, "recovery_wrapped_key"))
        db.add(vk)
    else:
        if body.get("replace") is not True:
            raise HTTPException(409, "Vault já existe")
        vk.kdf_iterations, vk.salt, vk.wrapped_key = iterations, salt, wrapped
        if recovery:
            vk.recovery_wrapped_key = _b64(recovery, 255, "recovery_wrapped_key")
        vk.updated_at = utcnow()
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ pastas


@router.get("/api/vault/folders")
def list_folders(user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(VaultFolder).where(VaultFolder.user_id == user.id).order_by(VaultFolder.id)).all()
    return [{"id": f.id, "name_enc": f.name_enc} for f in rows]


@router.post("/api/vault/folders", dependencies=[Depends(csrf_protect)])
async def create_folder(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    body = await request.json()
    if not db.get(VaultKey, user.id):
        raise HTTPException(400, "Crie o vault primeiro")
    f = VaultFolder(user_id=user.id, name_enc=_b64(body.get("name_enc"), 1024, "name_enc"))
    db.add(f)
    db.commit()
    return {"id": f.id}


@router.delete("/api/vault/folders/{fid}", dependencies=[Depends(csrf_protect)])
def delete_folder(fid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    f = db.get(VaultFolder, fid)
    if not f or f.user_id != user.id:
        raise HTTPException(404, "not_found")
    db.delete(f)  # itens voltam para a raiz (ON DELETE SET NULL)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ itens


def _item(db: Session, iid: int, user: User) -> VaultItem:
    it = db.get(VaultItem, iid)
    if not it or it.user_id != user.id:
        raise HTTPException(404, "not_found")
    return it


@router.get("/api/vault/items")
def list_items(user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(VaultItem).where(VaultItem.user_id == user.id).order_by(VaultItem.id.desc())).all()
    return [
        {"id": i.id, "folder_id": i.folder_id, "meta": i.meta, "size": i.size, "created_at": i.created_at.isoformat()}
        for i in rows
    ]


@router.post("/api/vault/items", dependencies=[Depends(csrf_protect)])
async def upload_item(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    s = get_settings()
    if not db.get(VaultKey, user.id):
        raise HTTPException(400, "Crie o vault primeiro")
    form = await request.form()
    blob, thumb = form.get("blob"), form.get("thumb")
    if not isinstance(blob, UploadFile) or not isinstance(thumb, UploadFile):
        raise HTTPException(400, "Envie blob e thumb cifrados")
    data = await blob.read(s.max_upload_bytes + 1)
    tdata = await thumb.read(2 * 1024 * 1024 + 1)
    if len(data) > s.max_upload_bytes:
        raise HTTPException(413, f"Arquivo cifrado maior que {s.max_upload_mb} MB")
    if len(tdata) > 2 * 1024 * 1024 or len(data) < 28 or len(tdata) < 28:  # 12 (IV) + 16 (tag GCM)
        raise HTTPException(400, "Blob inválido")
    used = db.scalar(select(func.coalesce(func.sum(VaultItem.size), 0)).where(VaultItem.user_id == user.id))
    if used + len(data) > VAULT_QUOTA_BYTES:
        raise HTTPException(413, "Cota do vault esgotada")
    folder = str(form.get("folder_id", ""))
    folder_id = None
    if folder.isdigit():
        f = db.get(VaultFolder, int(folder))
        if not f or f.user_id != user.id:
            raise HTTPException(404, "Pasta não encontrada")
        folder_id = f.id
    meta = _b64(str(form.get("meta", "")), MAX_META_LEN, "meta")

    storage = get_storage()
    try:
        # nomes e legendas neutros: o Telegram não vê nada além de bytes aleatórios
        stored = await storage.put(data, "vault.bin", "application/octet-stream", "vault")
        stored_t = await storage.put(tdata, "vault_t.bin", "application/octet-stream", "vault")
    except StorageError as e:
        raise HTTPException(502, f"Falha ao armazenar: {e}") from None
    it = VaultItem(
        user_id=user.id, folder_id=folder_id, meta=meta, size=len(data), storage_backend=storage.name,
        file_ref=stored.ref, file_message_id=stored.message_id,
        thumb_ref=stored_t.ref, thumb_message_id=stored_t.message_id,
    )
    db.add(it)
    db.commit()
    return {"id": it.id}


@router.patch("/api/vault/items/{iid}", dependencies=[Depends(csrf_protect)])
async def move_item(iid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    it = _item(db, iid, user)
    body = await request.json()
    fid = body.get("folder_id")
    if fid is None:
        it.folder_id = None
    else:
        f = db.get(VaultFolder, int(fid))
        if not f or f.user_id != user.id:
            raise HTTPException(404, "Pasta não encontrada")
        it.folder_id = f.id
    db.commit()
    return {"ok": True}


async def _blob(ref: str) -> Response:
    try:
        path = await fetch_cached(ref)
    except StorageError as e:
        raise HTTPException(502, str(e)) from None
    return Response(path.read_bytes(), media_type="application/octet-stream",
                    headers={"Cache-Control": "private, max-age=86400"})


@router.get("/api/vault/items/{iid}/blob")
async def get_blob(iid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return await _blob(_item(db, iid, user).file_ref)


@router.get("/api/vault/items/{iid}/thumb")
async def get_thumb(iid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return await _blob(_item(db, iid, user).thumb_ref)


@router.delete("/api/vault/items/{iid}", dependencies=[Depends(csrf_protect)])
async def delete_item(iid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    it = _item(db, iid, user)
    storage = get_storage()
    for ref, mid in ((it.file_ref, it.file_message_id), (it.thumb_ref, it.thumb_message_id)):
        try:
            await storage.delete(ref, mid)
        except StorageError:
            pass
    db.delete(it)
    db.commit()
    return {"ok": True}
