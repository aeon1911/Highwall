import base64
import os
import re

from tests.conftest import csrf_of, make_image, new_client, register


def upload(c, img, **fields):
    tok = csrf_of(c, "/upload")
    data = {"csrf_token": tok, "confirm_adults": "yes", "purity": "sketchy", **fields}
    return c.post("/upload", data=data, files={"file": ("w.jpg", img, "image/jpeg")}, headers={"X-CSRF-Token": tok})


def test_full_flow(appclient):
    # --- age gate
    anon = appclient
    r = anon.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/age-check")
    assert anon.get("/api/search").status_code == 403

    admin = new_client()
    assert admin.get("/").status_code == 200
    register(admin, "chefe")  # primeiro usuário vira admin
    assert "Moderação" in admin.get("/").text

    alice = new_client()
    register(alice, "alice")

    # --- CSRF obrigatório
    r = alice.post("/upload", files={"file": ("w.jpg", make_image(), "image/jpeg")})
    assert r.status_code == 403

    # --- upload abaixo de 2K é recusado
    r = upload(alice, make_image(1920, 1080))
    assert r.status_code == 422 and "2K" in r.json()["error"]

    # --- upload válido: usuário comum fica pendente
    r = upload(alice, make_image(3840, 2160, color=(255, 102, 153)),
               tags="ruiva, lingerie preta, tag-nova", characters="Lara Teste (Série X)", category="1")
    assert r.status_code == 200, r.text
    j = r.json()
    wid = j["id"]
    assert j["status"] == "pending" and j["resolution"] == "3840x2160"

    # duplicata exata é recusada
    assert upload(alice, make_image(3840, 2160, color=(255, 102, 153))).status_code == 422

    # pendente não aparece para outros nem na busca
    assert admin.get("/api/search?q=ruiva").json()["meta"]["total"] == 0
    assert alice.get(f"/w/{wid}").status_code == 200  # dono vê

    # --- admin aprova
    tok = csrf_of(admin, "/admin")
    r = admin.post(f"/admin/review/{wid}", data={"csrf_token": tok, "decision": "approved", "purity": "sketchy"},
                   follow_redirects=False)
    assert r.status_code == 303

    # --- busca: tag direta, alias, implicação, exclusão, personagem, cor
    s = lambda q, c=admin: c.get("/api/search", params={"q": q}).json()["meta"]["total"]
    assert s("ruiva") == 1
    assert s("redhead") == 1            # alias
    assert s("lingerie") == 1           # implicada por "lingerie preta"
    assert s("ruiva -lingerie") == 0    # exclusão
    assert s("char:lara-teste-serie-x") == 1
    assert s("series:serie-x") == 1
    assert s("@alice") == 1
    assert s("+naoexiste") == 0
    assert s("atleast:3840x2160") == 1 and s("atleast:5120x2880") == 0
    assert s("ratio:16x9") == 1
    colors = admin.get("/api/search").json()["data"][0]["colors"]
    assert s(f"color:{colors[0][1:]}") == 1
    assert s(f"like:{wid}") == 0  # sem outros parecidos ainda

    page = admin.get(f"/w/{wid}").text
    assert "lingerie preta" in page and "implícita" in page

    # --- segundo wallpaper NSFW (admin publica direto)
    r = upload(admin, make_image(2560, 1440, color=(10, 200, 30)), tags="nude artístico, ruiva", purity="nsfw")
    nid = r.json()["id"]
    assert r.json()["status"] == "approved"
    assert s(f"like:{wid}") == 1
    assert s("nude") == 1  # implicação nude artístico -> nude

    # visitante sem login não vê explícito
    guest = new_client()
    assert s("ruiva", guest) == 1
    assert guest.get(f"/media/{nid}/thumb").status_code == 401
    assert guest.get(f"/media/{wid}/thumb").status_code == 200
    full = admin.get(f"/media/{nid}/full?download=1")
    assert full.status_code == 200 and "attachment" in full.headers["content-disposition"]

    # páginas HTML principais renderizam
    for path in ["/", "/search?q=ruiva&sorting=toplist", "/categories", "/c/fotografia", "/colors", "/tags",
                 "/tag/ruiva", "/characters", "/character/lara-teste-serie-x", "/series/serie-x", "/u/alice",
                 "/u/alice/favorites", "/random", "/admin", "/admin/reports", "/admin/categories", "/admin/users",
                 "/admin/tags/ruiva", "/gallery", "/vault", "/settings", "/upload", "/terms", "/nao-existe"]:
        r = admin.get(path)
        assert r.status_code in (200, 404), (path, r.status_code)
        assert "Traceback" not in r.text

    # --- favoritos
    tok = csrf_of(alice)
    assert alice.post(f"/w/{wid}/favorite", headers={"X-CSRF-Token": tok}).json() == {"favorited": True, "count": 1}

    # --- galeria privada com pastas e subpastas
    r = alice.post("/gallery/save", data={"wallpaper_id": wid, "new_folder": "Ruivas"},
                   headers={"X-CSRF-Token": tok, "Accept": "application/json"})
    folder = r.json()["folder"]
    assert r.json()["saved"]
    alice.post("/gallery/folders", data={"csrf_token": tok, "name": "Favoritas", "parent": folder["id"]})
    tree = alice.get(f"/gallery/api/folders-for/{wid}").json()
    assert [(f["name"], f["depth"], f["saved"]) for f in tree] == [("Ruivas", 0, True), ("Favoritas", 1, False)]
    assert f"/media/{wid}/thumb" in alice.get(f"/gallery/{folder['id']}").text
    assert admin.get(f"/gallery/{folder['id']}").status_code == 404  # pasta de outro usuário

    # --- denúncia de menor tira do ar imediatamente
    bob = new_client()
    register(bob, "bob")
    btok = csrf_of(bob)
    bob.post(f"/w/{wid}/report", data={"csrf_token": btok, "reason": "underage"}, follow_redirects=False)
    assert s("@alice") == 0
    assert "Pessoa aparenta" in admin.get("/admin/reports").text


def test_vault_server_is_blind():
    c = new_client()
    register(c, "vaultuser")
    tok = csrf_of(c)
    h = {"X-CSRF-Token": tok}
    assert c.get("/api/vault/key").json() == {"exists": False}

    b64 = lambda n: base64.b64encode(os.urandom(n)).decode()
    # sem chave de recuperação não cria
    assert c.post("/api/vault/key", json={"iterations": 600000, "salt": b64(16), "wrapped_key": b64(60)}, headers=h).status_code == 400
    # iterações fracas são recusadas
    assert c.post("/api/vault/key", json={"iterations": 1000, "salt": b64(16), "wrapped_key": b64(60),
                                          "recovery_wrapped_key": b64(60)}, headers=h).status_code == 400
    r = c.post("/api/vault/key", json={"iterations": 600000, "salt": b64(16), "wrapped_key": b64(60),
                                       "recovery_wrapped_key": b64(60)}, headers=h)
    assert r.status_code == 200
    assert c.get("/api/vault/key").json()["exists"] is True

    fid = c.post("/api/vault/folders", json={"name_enc": b64(40)}, headers=h).json()["id"]
    blob, thumb = os.urandom(5000), os.urandom(800)
    r = c.post("/api/vault/items", data={"meta": b64(100), "folder_id": str(fid)},
               files={"blob": ("blob", blob), "thumb": ("thumb", thumb)}, headers=h)
    iid = r.json()["id"]
    assert c.get(f"/api/vault/items/{iid}/blob").content == blob   # devolve exatamente os bytes cifrados
    assert c.get(f"/api/vault/items/{iid}/thumb").content == thumb
    assert c.get("/api/vault/items").json()[0]["folder_id"] == fid

    other = new_client()
    register(other, "intruso")
    assert other.get(f"/api/vault/items/{iid}/blob").status_code == 404
    assert other.get("/api/vault/items").json() == []

    assert c.delete(f"/api/vault/items/{iid}", headers=h).json() == {"ok": True}
