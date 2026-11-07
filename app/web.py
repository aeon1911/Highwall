from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request
from fastapi.templating import Jinja2Templates

from .config import get_settings
from .imaging import PALETTE
from .models import PURITIES
from .search import SORTS, TOP_RANGES
from .security import csrf_token

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_env = templates.env
_env.globals.update(
    site_name=get_settings().site_name,
    PALETTE=PALETTE,
    PURITIES=PURITIES,
    SORTS=SORTS,
    TOP_RANGES=TOP_RANGES,
    PURITY_LABELS={"sfw": "Leve", "sketchy": "Sensual", "nsfw": "Explícito"},
)


def _filesize(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _qs(params: dict, **override) -> str:
    merged = {**params, **override}
    flat = []
    for k, v in merged.items():
        if v in (None, "", []):
            continue
        if isinstance(v, (list, tuple)):
            flat.extend((k, x) for x in v)
        else:
            flat.append((k, v))
    return urlencode(flat)


def _text_color(hex_: str) -> str:
    r, g, b = (int(hex_[i : i + 2], 16) for i in (1, 3, 5))
    return "#000" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#fff"


_env.filters["filesize"] = _filesize
_env.filters["textcolor"] = _text_color
_env.globals["qs"] = _qs


def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200):
    ctx = dict(ctx or {})
    if not hasattr(request.state, "user"):
        from .db import SessionLocal
        from .security import current_user

        with SessionLocal() as db:
            current_user(request, db)
    ctx.setdefault("user", request.state.user)
    ctx["csrf_token"] = csrf_token(request)
    ctx["flash"] = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def safe_next(url: str | None, default: str = "/") -> str:
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url:
        return default
    return url
