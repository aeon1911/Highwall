from __future__ import annotations

import hmac
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import date

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import User

_ph = PasswordHasher()

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(hashed: str, pw: str) -> bool:
    try:
        return _ph.verify(hashed, pw)
    except (VerifyMismatchError, InvalidHashError):
        return False


def age_on(birth: date, today: date | None = None) -> int:
    today = today or date.today()
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def validate_password(pw: str) -> str | None:
    if len(pw) < 10:
        return "A senha precisa ter pelo menos 10 caracteres."
    if len(pw) > 256:
        return "Senha longa demais."
    if pw.isdigit() or pw.isalpha():
        return "Misture letras com números ou símbolos."
    return None


# ------------------------------------------------------------------ sessão / usuário atual


def login_session(request: Request, user: User) -> None:
    request.session.clear()
    request.session["uid"] = user.id
    request.session["pwv"] = user.password_hash[-16:]  # invalida sessão se a senha mudar
    request.session["csrf"] = secrets.token_urlsafe(32)
    request.session["age_ok"] = True


def logout_session(request: Request) -> None:
    age_ok = request.session.get("age_ok")
    request.session.clear()
    if age_ok:
        request.session["age_ok"] = True


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    if hasattr(request.state, "user"):
        return request.state.user
    user = None
    uid = request.session.get("uid")
    if uid:
        user = db.get(User, uid)
        if user is None or user.is_banned or user.password_hash[-16:] != request.session.get("pwv"):
            request.session.pop("uid", None)
            user = None
    request.state.user = user
    return user


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="login_required")
    return user


def require_staff(user: User = Depends(require_user)) -> User:
    if not user.is_staff:
        raise HTTPException(status_code=403, detail="Acesso restrito à moderação.")
    return user


# ------------------------------------------------------------------ CSRF


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


async def csrf_protect(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.headers.get("x-csrf-token")
    if not sent:
        ctype = request.headers.get("content-type", "")
        if "form" in ctype:
            sent = (await request.form()).get("csrf_token")
    expected = request.session.get("csrf")
    if not expected or not sent or not hmac.compare_digest(str(sent), expected):
        raise HTTPException(status_code=403, detail="Token CSRF inválido. Recarregue a página.")


# ------------------------------------------------------------------ rate limit simples


class RateLimiter:
    """Janela deslizante em memória (suficiente para uma instância; use Redis se escalar)."""

    def __init__(self, limit: int, window_s: int):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


login_limiter = RateLimiter(limit=8, window_s=300)
register_limiter = RateLimiter(limit=5, window_s=3600)
upload_limiter = RateLimiter(limit=30, window_s=3600)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def min_age() -> int:
    return get_settings().min_age
