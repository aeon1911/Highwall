from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import SessionLocal, init_db
from .routes import admin, api, auth, browse, gallery, media, vault, wallpapers
from .seed import seed_defaults
from .web import render

# rotas acessíveis sem confirmar 18+
AGE_GATE_EXEMPT = ("/age-check", "/static/", "/favicon.ico", "/robots.txt", "/terms")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    Path(s.cache_dir).mkdir(parents=True, exist_ok=True)
    init_db()
    with SessionLocal() as db:
        seed_defaults(db)
    yield


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=s.site_name, lifespan=lifespan, docs_url="/api/docs" if s.debug else None, redoc_url=None)

    @app.middleware("http")
    async def age_gate_and_headers(request: Request, call_next):
        path = request.url.path
        if not path.startswith(AGE_GATE_EXEMPT) and not request.session.get("age_ok"):
            if path.startswith(("/api/", "/media/")):
                return JSONResponse({"detail": "age_verification_required"}, status_code=403)
            nxt = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/age-check?next={quote(nxt)}", status_code=303)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; frame-ancestors 'none'; form-action 'self'",
        )
        # sinaliza conteúdo adulto para filtros parentais (padrão RTA)
        response.headers.setdefault("Rating", "RTA-5042-1996-1400-1577-RTA")
        return response

    # precisa ficar "por fora" do middleware acima para a sessão já estar disponível nele
    app.add_middleware(
        SessionMiddleware,
        secret_key=s.secret_key,
        session_cookie="high_session",
        max_age=s.session_max_age,
        same_site="lax",
        https_only=not s.debug,
    )

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    for r in (auth.router, browse.router, wallpapers.router, gallery.router, vault.router, media.router, api.router, admin.router):
        app.include_router(r)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        wants_json = request.url.path.startswith(("/api/", "/media/")) or "application/json" in request.headers.get(
            "accept", ""
        )
        if exc.status_code == 401 and not wants_json:
            nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
        if wants_json:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return render(request, "error.html", {"status": exc.status_code, "detail": exc.detail}, exc.status_code)

    @app.get("/robots.txt", include_in_schema=False)
    async def robots():
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse("User-agent: *\nDisallow: /media/\nDisallow: /admin\nDisallow: /api/\n")

    return app


app = create_app()
