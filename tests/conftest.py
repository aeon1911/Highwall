import base64
import io
import os
import re
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="high-test-"))
os.environ.update(
    HIGH_DATABASE_URL=f"sqlite:///{_tmp / 'test.db'}",
    HIGH_STORAGE_BACKEND="local",
    HIGH_LOCAL_STORAGE_DIR=str(_tmp / "files"),
    HIGH_CACHE_DIR=str(_tmp / "cache"),
    HIGH_DEBUG="true",
    HIGH_SECRET_KEY="test-secret",
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.main import app  # noqa: E402


def make_image(w=2560, h=1440, color=(220, 30, 60), fmt="JPEG", exif=None) -> bytes:
    img = Image.new("RGB", (w, h), color)
    # um gradiente para o dHash não ser trivial e cada imagem ser diferente
    for x in range(0, w, max(1, w // 64)):
        for y in range(0, h, max(1, h // 8)):
            img.putpixel((x, y), ((x * 7) % 256, (y * 3) % 256, (x + y) % 256))
    img.paste((color[2], color[0], color[1]), (0, 0, w // 3, h // 2))
    buf = io.BytesIO()
    kw = {"exif": exif} if exif is not None else {}
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def csrf_of(client: TestClient, path="/") -> str:
    html = client.get(path).text
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def b64r(n: int) -> str:
    return base64.b64encode(os.urandom(n)).decode()


def key_material(auth: str | None = None) -> dict:
    """Simula o que o navegador envia (o servidor não consegue checar o conteúdo, só o formato)."""
    return {
        "auth_hash": auth or b64r(32),
        "kdf_salt": b64r(16),
        "kdf_iterations": 600000,
        "wrapped_key": b64r(60),
        "recovery_wrapped_key": b64r(60),
        "recovery_auth": b64r(32),
    }


def register(client: TestClient, username: str, material: dict | None = None) -> dict:
    tok = csrf_of(client, "/register")
    material = material or key_material()
    r = client.post(
        "/register",
        json={"username": username, "email": f"{username}@ex.com", "birthdate": "1995-05-05",
              "accept_terms": True, **material},
        headers={"X-CSRF-Token": tok},
    )
    assert r.status_code == 200 and r.json()["ok"], r.text
    return material


@pytest.fixture(scope="session")
def appclient():
    with TestClient(app) as c:
        yield c


def new_client() -> TestClient:
    c = TestClient(app)
    c.cookies.clear()
    tok = csrf_of_age(c)
    c.post("/age-check", data={"csrf_token": tok, "confirm": "yes", "next": "/"}, follow_redirects=False)
    return c


def csrf_of_age(c: TestClient) -> str:
    html = c.get("/age-check").text
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.fixture(scope="session", autouse=True)
def _database(appclient):
    """Garante tabelas e dados iniciais antes de qualquer teste (o lifespan roda no appclient)."""
    yield


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    from app import security
    from app.routes import auth

    for lim in (security.login_limiter, security.register_limiter, security.upload_limiter,
                auth.recover_limiter, auth.prelogin_limiter):
        lim.hits.clear()
    yield
