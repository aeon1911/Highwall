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


def register(client: TestClient, username: str, password="senha-forte-123") -> None:
    tok = csrf_of(client, "/register")
    r = client.post(
        "/register",
        data={"csrf_token": tok, "username": username, "email": f"{username}@ex.com", "password": password,
              "password2": password, "birthdate": "1995-05-05", "accept_terms": "yes"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text


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
