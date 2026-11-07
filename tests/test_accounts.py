from app.db import SessionLocal
from app.models import User, VaultKey
from app.security import hash_password
from tests.conftest import b64r, csrf_of, key_material, new_client, register


def post(c, path, json):
    return c.post(path, json=json, headers={"X-CSRF-Token": csrf_of(c)})


def test_server_never_gets_password_and_login_uses_auth_hash():
    c = new_client()
    m = register(c, "zk_user")
    with SessionLocal() as db:
        u = db.query(User).filter_by(username="zk_user").one()
        assert u.kdf_salt == m["kdf_salt"] and u.password_hash.startswith("$argon2")
        assert db.get(VaultKey, u.id).wrapped_key == m["wrapped_key"]  # vault já nasce no cadastro

    other = new_client()
    pre = other.get("/api/prelogin", params={"login": "zk_user"}).json()
    assert pre == {"scheme": "client", "salt": m["kdf_salt"], "iterations": 600000}
    # login inexistente recebe salt falso, estável (não revela quem tem conta)
    fake1 = other.get("/api/prelogin", params={"login": "ninguem"}).json()
    assert fake1["scheme"] == "client" and fake1 == other.get("/api/prelogin", params={"login": "ninguem"}).json()

    assert post(other, "/login", {"login": "zk_user", "auth_hash": b64r(32)}).status_code == 400
    assert post(other, "/login", {"login": "zk_user", "password": "qualquer"}).status_code == 400
    r = post(other, "/login", {"login": "zk_user", "auth_hash": m["auth_hash"], "next": "/vault"})
    assert r.json() == {"ok": True, "redirect": "/vault"}


def test_register_requires_key_material():
    c = new_client()
    body = {"username": "semchave", "email": "semchave@ex.com", "birthdate": "1990-01-01", "accept_terms": True}
    assert post(c, "/register", body).status_code == 400
    assert post(c, "/api/register/check", body).json() == {"ok": True, "errors": []}
    minor = {**body, "birthdate": "2015-01-01"}
    assert "18" in post(c, "/api/register/check", minor).json()["errors"][0]


def test_recover_with_words_and_change_password():
    c = new_client()
    m = register(c, "esquecido")
    thief = new_client()
    assert post(thief, "/api/recover/start", {"login": "esquecido", "recovery_auth": b64r(32)}).status_code == 400
    assert post(thief, "/api/recover/finish", key_material()).status_code == 400  # sem passar pelo start

    r = post(thief, "/api/recover/start", {"login": "esquecido", "recovery_auth": m["recovery_auth"]})
    assert r.json() == {"recovery_wrapped_key": m["recovery_wrapped_key"]}
    new = key_material()
    del new["recovery_wrapped_key"], new["recovery_auth"]
    assert post(thief, "/api/recover/finish", new).json()["ok"]
    # senha antiga parou de valer; a nova funciona; frase de recuperação continua a mesma
    d = new_client()
    assert post(d, "/login", {"login": "esquecido", "auth_hash": m["auth_hash"]}).status_code == 400
    assert post(d, "/login", {"login": "esquecido", "auth_hash": new["auth_hash"]}).json()["ok"]
    with SessionLocal() as db:
        u = db.query(User).filter_by(username="esquecido").one()
        assert db.get(VaultKey, u.id).recovery_wrapped_key == m["recovery_wrapped_key"]

    # troca de senha exige a atual
    newer = key_material()
    del newer["recovery_wrapped_key"], newer["recovery_auth"]
    assert post(d, "/api/account/password", {"current_auth": b64r(32), **newer}).status_code == 400
    assert post(d, "/api/account/password", {"current_auth": new["auth_hash"], **newer}).json() == {"ok": True}
    assert d.get("/api/vault/key").json()["wrapped_key"] == newer["wrapped_key"]


def test_legacy_account_is_migrated():
    from datetime import date

    with SessionLocal() as db:
        db.add(User(username="antigo", email="antigo@ex.com", birthdate=date(1990, 1, 1),
                    password_hash=hash_password("senha-velha-123")))
        db.commit()
    c = new_client()
    assert c.get("/api/prelogin", params={"login": "antigo"}).json() == {"scheme": "legacy"}
    r = post(c, "/login", {"login": "antigo", "password": "senha-velha-123"})
    assert r.json()["redirect"] == "/setup-key"
    assert c.get("/vault", follow_redirects=False).headers["location"] == "/setup-key"
    assert post(c, "/api/account/verify-legacy", {"password": "errada"}).json() == {"ok": False}
    m = key_material()
    assert post(c, "/api/account/setup-key", {"password": "errada", **m}).status_code == 400
    assert post(c, "/api/account/setup-key", {"password": "senha-velha-123", **m}).json()["redirect"] == "/vault"
    d = new_client()
    assert d.get("/api/prelogin", params={"login": "antigo"}).json()["scheme"] == "client"
    assert post(d, "/login", {"login": "antigo", "password": "senha-velha-123"}).status_code == 400
    assert post(d, "/login", {"login": "antigo", "auth_hash": m["auth_hash"]}).json()["ok"]
