"""Contas no modelo "conhecimento zero" (como o Bitwarden).

A senha real NUNCA sai do navegador. Lá ela vira, via PBKDF2 (600 mil it.) + HKDF:
  auth_hash  → enviado ao servidor, que guarda argon2(auth_hash) e usa só para login
  KEK        → fica no navegador e desembrulha a chave-mestra do vault

No cadastro o navegador também gera a chave-mestra do vault e as 12 palavras BIP39
(a frase de recuperação). Das palavras saem a chave de recuperação (embrulha a mesma
chave-mestra) e o recovery_auth (prova ao servidor que a pessoa tem as palavras).
Com as 12 palavras dá para redefinir a senha sem perder nada do vault.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import PURITIES, User, VaultKey, utcnow
from ..security import (
    EMAIL_RE,
    USERNAME_RE,
    RateLimiter,
    age_on,
    client_ip,
    csrf_protect,
    current_user,
    hash_password,
    login_limiter,
    login_session,
    logout_session,
    min_age,
    register_limiter,
    require_user,
    verify_password,
)
from ..web import flash, render, safe_next

router = APIRouter()

KDF_ITERATIONS = 600_000
MIN_ITERATIONS = 300_000
RECOVER_WINDOW_S = 600
recover_limiter = RateLimiter(limit=6, window_s=900)
prelogin_limiter = RateLimiter(limit=60, window_s=300)


# ------------------------------------------------------------------ validação dos valores do navegador


def b64field(body: dict, name: str, nbytes: int | tuple[int, int]) -> str:
    raw = body.get(name)
    if not isinstance(raw, str) or len(raw) > 400:
        raise HTTPException(400, f"Campo {name} ausente ou inválido.")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, f"Campo {name} inválido.") from None
    lo, hi = (nbytes, nbytes) if isinstance(nbytes, int) else nbytes
    if not lo <= len(decoded) <= hi:
        raise HTTPException(400, f"Campo {name} com tamanho inválido.")
    return raw


def iterations_field(body: dict) -> int:
    try:
        it = int(body.get("kdf_iterations", 0))
    except (TypeError, ValueError):
        it = 0
    if not MIN_ITERATIONS <= it <= 10_000_000:
        raise HTTPException(400, "Número de iterações fora do permitido.")
    return it


def key_material(body: dict, with_recovery: bool) -> dict:
    """Valida o pacote de chaves que o navegador envia."""
    out = {
        "auth_hash": b64field(body, "auth_hash", 32),
        "kdf_salt": b64field(body, "kdf_salt", 16),
        "kdf_iterations": iterations_field(body),
        "wrapped_key": b64field(body, "wrapped_key", 60),  # IV 12 + chave 32 + tag 16
    }
    if with_recovery:
        out["recovery_wrapped_key"] = b64field(body, "recovery_wrapped_key", 60)
        out["recovery_auth"] = b64field(body, "recovery_auth", 32)
    return out


def apply_material(db: Session, user: User, m: dict) -> None:
    user.password_hash = hash_password(m["auth_hash"])
    user.kdf_salt = m["kdf_salt"]
    user.kdf_iterations = m["kdf_iterations"]
    vk = db.get(VaultKey, user.id)
    if vk is None:
        vk = VaultKey(user_id=user.id, wrapped_key=m["wrapped_key"], recovery_wrapped_key=m["recovery_wrapped_key"])
        db.add(vk)
    vk.wrapped_key = m["wrapped_key"]
    if "recovery_wrapped_key" in m:
        vk.recovery_wrapped_key = m["recovery_wrapped_key"]
        user.recovery_auth_hash = hash_password(m["recovery_auth"])
    vk.updated_at = utcnow()


def find_user(db: Session, login: str) -> User | None:
    login = (login or "").strip()
    if not login:
        return None
    return db.scalar(select(User).where((func.lower(User.username) == login.lower()) | (User.email == login.lower())))


def fake_salt(login: str) -> str:
    """Salt determinístico para logins inexistentes (não revela quem tem conta)."""
    key = get_settings().secret_key.encode()
    return base64.b64encode(hmac.new(key, b"prelogin:" + login.lower().encode(), hashlib.sha256).digest()[:16]).decode()


# ------------------------------------------------------------------ verificação de idade


@router.get("/age-check")
def age_check_page(request: Request, next: str = "/"):
    return render(request, "age_check.html", {"next": safe_next(next), "min_age": min_age()})


@router.post("/age-check", dependencies=[Depends(csrf_protect)])
async def age_check_submit(request: Request):
    form = await request.form()
    if form.get("confirm") != "yes":
        return RedirectResponse("https://www.google.com", status_code=303)
    request.session["age_ok"] = True
    return RedirectResponse(safe_next(str(form.get("next", "/"))), status_code=303)


@router.get("/terms")
def terms(request: Request):
    return render(request, "terms.html", {"min_age": min_age()})


# ------------------------------------------------------------------ prelogin


@router.get("/api/prelogin")
def prelogin(request: Request, login: str = "", db: Session = Depends(get_db)):
    if not prelogin_limiter.hit(client_ip(request)):
        raise HTTPException(429, "Muitas tentativas.")
    user = find_user(db, login)
    if user and not user.client_kdf:
        return {"scheme": "legacy"}
    if user:
        return {"scheme": "client", "salt": user.kdf_salt, "iterations": user.kdf_iterations}
    return {"scheme": "client", "salt": fake_salt(login.strip()), "iterations": KDF_ITERATIONS}


# ------------------------------------------------------------------ cadastro


def registration_errors(db: Session, body: dict) -> tuple[list[str], dict]:
    errors: list[str] = []
    username = str(body.get("username", "")).strip()
    email = str(body.get("email", "")).strip().lower()
    if not USERNAME_RE.match(username):
        errors.append("Usuário: 3 a 32 caracteres (letras, números, _ . -).")
    if not EMAIL_RE.match(email) or len(email) > 254:
        errors.append("E-mail inválido.")
    bdate = None
    try:
        bdate = date.fromisoformat(str(body.get("birthdate", "")))
        if bdate.year < 1900:
            errors.append("Data de nascimento inválida.")
        elif age_on(bdate) < min_age():
            errors.append(f"Você precisa ter pelo menos {min_age()} anos.")
    except ValueError:
        errors.append("Data de nascimento inválida.")
    if body.get("accept_terms") is not True:
        errors.append("Você precisa aceitar os termos de uso.")
    if not errors:
        if db.scalar(select(User.id).where(func.lower(User.username) == username.lower())):
            errors.append("Esse nome de usuário já está em uso.")
        if db.scalar(select(User.id).where(User.email == email)):
            errors.append("Esse e-mail já está cadastrado.")
    return errors, {"username": username, "email": email, "birthdate": bdate}


@router.get("/register")
def register_page(request: Request, user=Depends(current_user)):
    if user:
        return RedirectResponse("/", status_code=303)
    return render(request, "register.html", {"min_age": min_age(), "iterations": KDF_ITERATIONS})


@router.post("/api/register/check", dependencies=[Depends(csrf_protect)])
async def register_check(request: Request, db: Session = Depends(get_db)):
    """Valida os dados antes de o navegador gerar as chaves e mostrar as 12 palavras."""
    errors, _ = registration_errors(db, await request.json())
    return {"ok": not errors, "errors": errors}


@router.post("/register", dependencies=[Depends(csrf_protect)])
async def register_submit(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    errors, data = registration_errors(db, body)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, 400)
    material = key_material(body, with_recovery=True)
    if not register_limiter.hit(client_ip(request)):
        return JSONResponse({"ok": False, "errors": ["Muitos cadastros a partir deste IP. Tente mais tarde."]}, 429)

    is_first = db.scalar(select(func.count()).select_from(User)) == 0
    user = User(username=data["username"], email=data["email"], birthdate=data["birthdate"], password_hash="",
                role="admin" if is_first else "user")
    db.add(user)
    db.flush()
    apply_material(db, user, material)
    db.commit()
    login_session(request, user)
    flash(request, f"Bem-vindo(a), {user.username}! Seu vault já está pronto." + (" Você é o administrador." if is_first else ""), "ok")
    return {"ok": True, "redirect": "/"}


# ------------------------------------------------------------------ login


@router.get("/login")
def login_page(request: Request, next: str = "/", user=Depends(current_user)):
    if user:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", {"next": safe_next(next)})


@router.post("/login", dependencies=[Depends(csrf_protect)])
async def login_submit(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    login = str(body.get("login", "")).strip()
    nxt = safe_next(str(body.get("next", "/")))
    if not login_limiter.hit(client_ip(request)) or not login_limiter.hit(f"u:{login.lower()}"):
        return JSONResponse({"ok": False, "error": "Muitas tentativas. Aguarde alguns minutos."}, 429)
    user = find_user(db, login)
    bad = JSONResponse({"ok": False, "error": "Usuário ou senha incorretos."}, 400)
    if not user:
        verify_password(hash_password("x"), "y")  # tempo parecido com o de um usuário real
        return bad
    if user.client_kdf:
        secret = body.get("auth_hash")
    else:
        secret = body.get("password")  # conta antiga: senha crua, só até gerar as 12 palavras
    if not isinstance(secret, str) or not verify_password(user.password_hash, secret):
        return bad
    if user.is_banned:
        return JSONResponse({"ok": False, "error": "Esta conta está suspensa."}, 403)
    login_session(request, user)
    if not user.client_kdf:
        return {"ok": True, "redirect": "/setup-key", "legacy": True}
    return {"ok": True, "redirect": nxt}


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request):
    logout_session(request)
    return RedirectResponse("/", status_code=303)


# ------------------------------------------------------------------ contas antigas: gerar as 12 palavras


@router.get("/setup-key")
def setup_key_page(request: Request, user: User = Depends(require_user)):
    if user.client_kdf:
        return RedirectResponse("/vault", status_code=303)
    return render(request, "setup_key.html", {"iterations": KDF_ITERATIONS})


@router.post("/api/account/verify-legacy", dependencies=[Depends(csrf_protect)])
async def verify_legacy(request: Request, user: User = Depends(require_user)):
    body = await request.json()
    if user.client_kdf:
        raise HTTPException(409, "Conta já migrada.")
    if not login_limiter.hit(f"legacy:{user.id}"):
        raise HTTPException(429, "Muitas tentativas.")
    return {"ok": verify_password(user.password_hash, str(body.get("password", "")))}


@router.post("/api/account/setup-key", dependencies=[Depends(csrf_protect)])
async def setup_key(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    body = await request.json()
    if user.client_kdf:
        raise HTTPException(409, "Conta já migrada.")
    if not verify_password(user.password_hash, str(body.get("password", ""))):
        raise HTTPException(400, "Senha atual incorreta.")
    material = key_material(body, with_recovery=True)
    user = db.merge(user)
    apply_material(db, user, material)
    db.commit()
    login_session(request, user)
    return {"ok": True, "redirect": "/vault"}


# ------------------------------------------------------------------ troca de senha (mantém a chave-mestra)


@router.post("/api/account/password", dependencies=[Depends(csrf_protect)])
async def change_password(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    body = await request.json()
    if not user.client_kdf:
        raise HTTPException(409, "Gere sua frase de recuperação primeiro.")
    if not login_limiter.hit(f"pw:{user.id}"):
        raise HTTPException(429, "Muitas tentativas.")
    current = body.get("current_auth")
    if not isinstance(current, str) or not verify_password(user.password_hash, current):
        raise HTTPException(400, "Senha atual incorreta.")
    material = key_material(body, with_recovery=False)
    user = db.merge(user)
    apply_material(db, user, material)
    db.commit()
    login_session(request, user)
    return {"ok": True}


# ------------------------------------------------------------------ recuperação pelas 12 palavras


@router.get("/recover")
def recover_page(request: Request):
    return render(request, "recover.html", {"iterations": KDF_ITERATIONS})


@router.post("/api/recover/start", dependencies=[Depends(csrf_protect)])
async def recover_start(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    login = str(body.get("login", "")).strip()
    if not recover_limiter.hit(client_ip(request)) or not recover_limiter.hit(f"u:{login.lower()}"):
        raise HTTPException(429, "Muitas tentativas. Aguarde 15 minutos.")
    proof = b64field(body, "recovery_auth", 32)
    user = find_user(db, login)
    if not user or not user.recovery_auth_hash or not verify_password(user.recovery_auth_hash, proof):
        raise HTTPException(400, "Usuário ou palavras de recuperação incorretos.")
    vk = db.get(VaultKey, user.id)
    request.session["recover"] = {"uid": user.id, "t": int(time.time())}
    return {"recovery_wrapped_key": vk.recovery_wrapped_key}


@router.post("/api/recover/finish", dependencies=[Depends(csrf_protect)])
async def recover_finish(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    state = request.session.get("recover") or {}
    if not state or time.time() - state.get("t", 0) > RECOVER_WINDOW_S:
        raise HTTPException(400, "Recuperação expirada. Comece de novo.")
    user = db.get(User, state["uid"])
    if not user:
        raise HTTPException(400, "Conta não encontrada.")
    material = key_material(body, with_recovery=False)
    apply_material(db, user, material)
    db.commit()
    login_session(request, user)  # limpa a sessão de recuperação
    flash(request, "Senha redefinida. Seu vault continua intacto.", "ok")
    return {"ok": True, "redirect": "/vault"}


# ------------------------------------------------------------------ configurações


@router.get("/settings")
def settings_page(request: Request, user: User = Depends(require_user)):
    return render(request, "settings.html", {"prefs": user.purity_pref.split(","), "iterations": KDF_ITERATIONS})


@router.post("/settings", dependencies=[Depends(csrf_protect)])
async def settings_submit(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    form = await request.form()
    user = db.merge(user)
    prefs = [p for p in form.getlist("purity") if p in PURITIES] or ["sfw"]
    user.purity_pref = ",".join(prefs)
    user.bio = str(form.get("bio", ""))[:1000]
    db.commit()
    flash(request, "Preferências salvas.", "ok")
    return RedirectResponse("/settings", status_code=303)
