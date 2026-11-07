from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PURITIES, User
from ..security import (
    EMAIL_RE,
    USERNAME_RE,
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
    validate_password,
    verify_password,
)
from ..web import flash, render, safe_next

router = APIRouter()


# ------------------------------------------------------------------ verificação de idade


@router.get("/age-check")
def age_check_page(request: Request, next: str = "/"):
    return render(request, "age_check.html", {"next": safe_next(next), "min_age": min_age()})


@router.post("/age-check", dependencies=[Depends(csrf_protect)])
def age_check_submit(request: Request, confirm: str = Form(""), next: str = Form("/")):
    if confirm != "yes":
        return RedirectResponse("https://www.google.com", status_code=303)
    request.session["age_ok"] = True
    return RedirectResponse(safe_next(next), status_code=303)


@router.get("/terms")
def terms(request: Request):
    return render(request, "terms.html", {"min_age": min_age()})


# ------------------------------------------------------------------ cadastro


@router.get("/register")
def register_page(request: Request, user=Depends(current_user)):
    if user:
        return RedirectResponse("/", status_code=303)
    return render(request, "register.html", {"form": {}, "min_age": min_age()})


@router.post("/register", dependencies=[Depends(csrf_protect)])
def register_submit(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    birthdate: str = Form(...),
    accept_terms: str = Form(""),
    db: Session = Depends(get_db),
):
    form = {"username": username, "email": email, "birthdate": birthdate}
    errors: list[str] = []
    username, email = username.strip(), email.strip().lower()

    if not USERNAME_RE.match(username):
        errors.append("Usuário: 3 a 32 caracteres (letras, números, _ . -).")
    if not EMAIL_RE.match(email) or len(email) > 254:
        errors.append("E-mail inválido.")
    if (msg := validate_password(password)) is not None:
        errors.append(msg)
    if password != password2:
        errors.append("As senhas não conferem.")
    try:
        bdate = date.fromisoformat(birthdate)
        if age_on(bdate) < min_age():
            errors.append(f"Você precisa ter pelo menos {min_age()} anos.")
        elif bdate.year < 1900:
            errors.append("Data de nascimento inválida.")
    except ValueError:
        bdate = None
        errors.append("Data de nascimento inválida.")
    if accept_terms != "yes":
        errors.append("Você precisa aceitar os termos de uso.")
    if not errors:
        if db.scalar(select(User.id).where(func.lower(User.username) == username.lower())):
            errors.append("Esse nome de usuário já está em uso.")
        if db.scalar(select(User.id).where(User.email == email)):
            errors.append("Esse e-mail já está cadastrado.")
    if not errors and not register_limiter.hit(client_ip(request)):
        errors.append("Muitos cadastros a partir deste IP. Tente mais tarde.")

    if errors:
        return render(request, "register.html", {"form": form, "errors": errors, "min_age": min_age()}, 400)

    is_first = db.scalar(select(func.count()).select_from(User)) == 0
    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        birthdate=bdate,
        role="admin" if is_first else "user",  # o primeiro cadastro vira administrador
    )
    db.add(user)
    db.commit()
    login_session(request, user)
    flash(request, f"Bem-vindo(a), {user.username}!" + (" Você é o administrador." if is_first else ""), "ok")
    return RedirectResponse("/", status_code=303)


# ------------------------------------------------------------------ login


@router.get("/login")
def login_page(request: Request, next: str = "/", user=Depends(current_user)):
    if user:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", {"next": safe_next(next)})


@router.post("/login", dependencies=[Depends(csrf_protect)])
def login_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    login = login.strip()
    ctx = {"next": safe_next(next), "login": login}
    if not login_limiter.hit(client_ip(request)) or not login_limiter.hit(f"u:{login.lower()}"):
        return render(request, "login.html", {**ctx, "error": "Muitas tentativas. Aguarde alguns minutos."}, 429)

    user = db.scalar(
        select(User).where((func.lower(User.username) == login.lower()) | (User.email == login.lower()))
    )
    if not user or not verify_password(user.password_hash, password):
        return render(request, "login.html", {**ctx, "error": "Usuário ou senha incorretos."}, 400)
    if user.is_banned:
        return render(request, "login.html", {**ctx, "error": "Esta conta está suspensa."}, 403)
    login_session(request, user)
    return RedirectResponse(safe_next(next), status_code=303)


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request):
    logout_session(request)
    return RedirectResponse("/", status_code=303)


# ------------------------------------------------------------------ configurações da conta


@router.get("/settings")
def settings_page(request: Request, user: User = Depends(require_user)):
    return render(request, "settings.html", {"prefs": user.purity_pref.split(",")})


@router.post("/settings", dependencies=[Depends(csrf_protect)])
async def settings_submit(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    form = await request.form()
    user = db.merge(user)
    action = form.get("action")
    if action == "prefs":
        prefs = [p for p in form.getlist("purity") if p in PURITIES] or ["sfw"]
        user.purity_pref = ",".join(prefs)
        user.bio = str(form.get("bio", ""))[:1000]
        db.commit()
        flash(request, "Preferências salvas.", "ok")
    elif action == "password":
        current, new, new2 = (str(form.get(k, "")) for k in ("current", "new", "new2"))
        if not verify_password(user.password_hash, current):
            flash(request, "Senha atual incorreta.", "error")
        elif (msg := validate_password(new)) is not None:
            flash(request, msg, "error")
        elif new != new2:
            flash(request, "As novas senhas não conferem.", "error")
        else:
            user.password_hash = hash_password(new)
            db.commit()
            login_session(request, user)
            flash(request, "Senha alterada.", "ok")
    return RedirectResponse("/settings", status_code=303)
