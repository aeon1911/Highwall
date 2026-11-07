// Vault pessoal com criptografia de ponta a ponta (tudo acontece AQUI, no navegador).
//
//  senha do vault ──PBKDF2-SHA256(600k, salt)──▶ KEK
//  chave-mestra (32 bytes aleatórios) ──AES-GCM(KEK)──▶ wrapped_key  (servidor guarda)
//  chave de recuperação (32 bytes, mostrada 1x) ──AES-GCM──▶ recovery_wrapped_key
//  arquivo / miniatura / metadados / nome de pasta ──AES-GCM(chave-mestra)──▶ servidor/Telegram
//
// Formato de todo ciphertext: IV (12 bytes) || AES-GCM(ciphertext + tag 16 bytes).
// Um AAD diferente por tipo de dado impede trocar a miniatura de um item pelo arquivo de outro.
"use strict";

(() => {
  const root = document.getElementById("vault");
  if (!root) return;

  const ITERATIONS = 600000;
  const AUTO_LOCK_MS = 15 * 60 * 1000;
  const enc = new TextEncoder();
  const dec = new TextDecoder();
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const $ = (sel) => root.querySelector(sel);

  let masterKey = null; // CryptoKey não-extraível, só em memória
  let keyInfo = null;
  let folders = []; // {id, name}
  let items = []; // {id, folder_id, meta:{...}, size}
  let currentFolder = null;
  const objectUrls = new Set();
  let lockTimer = null;

  // ------------------------------------------------------------ utilidades
  const b64 = (buf) => {
    const bytes = new Uint8Array(buf);
    let s = "";
    for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(s);
  };
  const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  const rand = (n) => crypto.getRandomValues(new Uint8Array(n));
  const concat = (a, b) => {
    const out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  };
  const AAD = (kind) => enc.encode(`high-vault:v1:${kind}`);
  const fmtSize = (n) => (n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : (n / 1024).toFixed(0) + " KB");

  async function api(path, opts = {}) {
    const headers = { "X-CSRF-Token": csrf, ...(opts.headers || {}) };
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
    }
    const r = await fetch(path, { credentials: "same-origin", ...opts, headers });
    if (!r.ok) {
      let msg = r.status;
      try { msg = (await r.json()).detail || msg; } catch (_) {}
      throw new Error(msg);
    }
    return r;
  }

  // ------------------------------------------------------------ primitivas de criptografia
  async function deriveKEK(passphrase, salt, iterations) {
    const base = await crypto.subtle.importKey("raw", enc.encode(passphrase.normalize("NFKC")), "PBKDF2", false, ["deriveKey"]);
    return crypto.subtle.deriveKey(
      { name: "PBKDF2", hash: "SHA-256", salt, iterations },
      base,
      { name: "AES-GCM", length: 256 },
      false,
      ["encrypt", "decrypt"]
    );
  }
  const importAes = (raw, extractable = false) =>
    crypto.subtle.importKey("raw", raw, { name: "AES-GCM" }, extractable, ["encrypt", "decrypt"]);

  async function seal(key, data, kind) {
    const iv = rand(12);
    const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: AAD(kind) }, key, data);
    return concat(iv, new Uint8Array(ct));
  }
  async function open(key, sealed, kind) {
    const bytes = sealed instanceof Uint8Array ? sealed : new Uint8Array(sealed);
    return crypto.subtle.decrypt(
      { name: "AES-GCM", iv: bytes.subarray(0, 12), additionalData: AAD(kind) },
      key,
      bytes.subarray(12)
    );
  }
  const sealText = async (key, obj, kind) => b64(await seal(key, enc.encode(JSON.stringify(obj)), kind));
  const openText = async (key, s, kind) => JSON.parse(dec.decode(await open(key, unb64(s), kind)));

  // chave de recuperação: 32 bytes em hex, agrupados de 4 em 4 para facilitar anotar
  const formatRecovery = (raw) => [...raw].map((b) => b.toString(16).padStart(2, "0")).join("").match(/.{4}/g).join("-");
  const parseRecovery = (s) => {
    const hex = s.replace(/[^0-9a-f]/gi, "");
    if (hex.length !== 64) throw new Error("Chave de recuperação deve ter 64 caracteres hexadecimais.");
    return Uint8Array.from(hex.match(/.{2}/g), (h) => parseInt(h, 16));
  };

  // ------------------------------------------------------------ telas
  function show(id) {
    root.querySelectorAll("[data-screen]").forEach((el) => (el.hidden = el.dataset.screen !== id));
  }
  function setMsg(sel, text, kind = "error") {
    const el = $(sel);
    el.textContent = text;
    el.className = "msg " + kind;
    el.hidden = !text;
  }
  function busy(form, on) {
    form.querySelectorAll("button, input").forEach((el) => (el.disabled = on));
  }

  async function init() {
    if (!window.isSecureContext || !crypto.subtle) {
      show("insecure");
      return;
    }
    keyInfo = await (await api("/api/vault/key")).json();
    show(keyInfo.exists ? "unlock" : "setup");
  }

  // ---- criação
  $("#setup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const p1 = f.pass.value, p2 = f.pass2.value;
    if (p1.length < 12) return setMsg("#setup-msg", "Use pelo menos 12 caracteres.");
    if (p1 !== p2) return setMsg("#setup-msg", "As senhas não conferem.");
    busy(f, true);
    setMsg("#setup-msg", "Gerando chaves (pode levar alguns segundos)…", "info");
    try {
      const masterRaw = rand(32);
      const recoveryRaw = rand(32);
      const salt = rand(16);
      const kek = await deriveKEK(p1, salt, ITERATIONS);
      const wrapped = await seal(kek, masterRaw, "master");
      const recWrapped = await seal(await importAes(recoveryRaw), masterRaw, "master");
      await api("/api/vault/key", {
        method: "POST",
        json: { iterations: ITERATIONS, salt: b64(salt), wrapped_key: b64(wrapped), recovery_wrapped_key: b64(recWrapped) },
      });
      masterKey = await importAes(masterRaw);
      masterRaw.fill(0);
      $("#recovery-code").textContent = formatRecovery(recoveryRaw);
      recoveryRaw.fill(0);
      f.reset();
      keyInfo = await (await api("/api/vault/key")).json();
      show("recovery");
    } catch (err) {
      setMsg("#setup-msg", "Erro: " + err.message);
    } finally {
      busy(f, false);
    }
  });
  $("#recovery-done").addEventListener("click", () => {
    if (!$("#recovery-ack").checked) return alert("Confirme que guardou a chave de recuperação.");
    $("#recovery-code").textContent = "";
    enterVault();
  });

  // ---- desbloqueio
  $("#unlock-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    busy(f, true);
    setMsg("#unlock-msg", "Derivando chave…", "info");
    try {
      const kek = await deriveKEK(f.pass.value, unb64(keyInfo.salt), keyInfo.iterations);
      let raw;
      try {
        raw = new Uint8Array(await open(kek, unb64(keyInfo.wrapped_key), "master"));
      } catch (_) {
        throw new Error("Senha do vault incorreta.");
      }
      masterKey = await importAes(raw);
      raw.fill(0);
      f.reset();
      setMsg("#unlock-msg", "");
      enterVault();
    } catch (err) {
      setMsg("#unlock-msg", err.message);
    } finally {
      busy(f, false);
    }
  });

  // ---- recuperação: abre com a chave de recuperação e obriga a definir senha nova
  $("#recover-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.pass.value.length < 12) return setMsg("#recover-msg", "Nova senha: mínimo 12 caracteres.");
    if (f.pass.value !== f.pass2.value) return setMsg("#recover-msg", "As senhas não conferem.");
    busy(f, true);
    try {
      const recKey = await importAes(parseRecovery(f.code.value));
      let raw;
      try {
        raw = new Uint8Array(await open(recKey, unb64(keyInfo.recovery_wrapped_key), "master"));
      } catch (_) {
        throw new Error("Chave de recuperação incorreta.");
      }
      const salt = rand(16);
      const kek = await deriveKEK(f.pass.value, salt, ITERATIONS);
      await api("/api/vault/key", {
        method: "POST",
        json: { replace: true, iterations: ITERATIONS, salt: b64(salt), wrapped_key: b64(await seal(kek, raw, "master")) },
      });
      masterKey = await importAes(raw);
      raw.fill(0);
      f.reset();
      keyInfo = await (await api("/api/vault/key")).json();
      enterVault();
    } catch (err) {
      setMsg("#recover-msg", err.message);
    } finally {
      busy(f, false);
    }
  });
  $("#show-recover").addEventListener("click", () => show("recover"));
  $("#back-unlock").addEventListener("click", () => show("unlock"));

  // ---- trocar senha (re-embrulha a mesma chave-mestra; nenhum arquivo precisa ser recifrado)
  $("#change-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.pass.value.length < 12) return setMsg("#change-msg", "Mínimo 12 caracteres.");
    if (f.pass.value !== f.pass2.value) return setMsg("#change-msg", "As senhas não conferem.");
    busy(f, true);
    try {
      const oldKek = await deriveKEK(f.old.value, unb64(keyInfo.salt), keyInfo.iterations);
      let raw;
      try {
        raw = new Uint8Array(await open(oldKek, unb64(keyInfo.wrapped_key), "master"));
      } catch (_) {
        throw new Error("Senha atual incorreta.");
      }
      const salt = rand(16);
      const kek = await deriveKEK(f.pass.value, salt, ITERATIONS);
      await api("/api/vault/key", {
        method: "POST",
        json: { replace: true, iterations: ITERATIONS, salt: b64(salt), wrapped_key: b64(await seal(kek, raw, "master")) },
      });
      raw.fill(0);
      keyInfo = await (await api("/api/vault/key")).json();
      f.reset();
      setMsg("#change-msg", "Senha do vault alterada.", "ok");
    } catch (err) {
      setMsg("#change-msg", err.message);
    } finally {
      busy(f, false);
    }
  });

  // ------------------------------------------------------------ vault aberto
  function lock() {
    masterKey = null;
    items = [];
    folders = [];
    objectUrls.forEach((u) => URL.revokeObjectURL(u));
    objectUrls.clear();
    $("#grid").innerHTML = "";
    closeViewer();
    show("unlock");
  }
  function bumpLock() {
    clearTimeout(lockTimer);
    lockTimer = setTimeout(lock, AUTO_LOCK_MS);
  }
  ["click", "keydown", "mousemove", "touchstart"].forEach((ev) =>
    document.addEventListener(ev, () => masterKey && bumpLock(), { passive: true })
  );
  $("#lock-btn").addEventListener("click", lock);

  async function enterVault() {
    show("open");
    bumpLock();
    await reload();
  }

  async function reload() {
    const [fRes, iRes] = await Promise.all([api("/api/vault/folders"), api("/api/vault/items")]);
    folders = [];
    for (const f of await fRes.json()) {
      try {
        folders.push({ id: f.id, name: (await openText(masterKey, f.name_enc, "folder")).name });
      } catch (_) {
        folders.push({ id: f.id, name: "(pasta ilegível)" });
      }
    }
    items = [];
    for (const it of await iRes.json()) {
      try {
        items.push({ ...it, meta: await openText(masterKey, it.meta, "meta") });
      } catch (_) {
        items.push({ ...it, meta: { name: "(ilegível)", type: "application/octet-stream" } });
      }
    }
    const used = items.reduce((a, b) => a + b.size, 0);
    $("#quota").textContent = `${fmtSize(used)} de ${fmtSize(keyInfo.quota_bytes)}`;
    renderFolders();
    renderGrid();
  }

  function renderFolders() {
    const nav = $("#folders");
    nav.innerHTML = "";
    const mk = (id, name, count) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip" + (currentFolder === id ? " active" : "");
      b.textContent = `${name} (${count})`;
      b.addEventListener("click", () => {
        currentFolder = id;
        renderFolders();
        renderGrid();
      });
      nav.appendChild(b);
    };
    mk(null, "Tudo", items.length);
    folders.forEach((f) => mk(f.id, f.name, items.filter((i) => i.folder_id === f.id).length));
    const sel = $("#upload-folder");
    sel.innerHTML = '<option value="">(sem pasta)</option>';
    folders.forEach((f) => sel.insertAdjacentHTML("beforeend", `<option value="${f.id}"></option>`));
    [...sel.options].slice(1).forEach((o, i) => (o.textContent = folders[i].name));
    sel.value = currentFolder ?? "";
    $("#delete-folder").hidden = currentFolder === null;
  }

  const thumbCache = new Map();
  async function thumbUrl(item) {
    if (thumbCache.has(item.id)) return thumbCache.get(item.id);
    const r = await api(`/api/vault/items/${item.id}/thumb`);
    const plain = await open(masterKey, await r.arrayBuffer(), "thumb");
    const url = URL.createObjectURL(new Blob([plain], { type: "image/webp" }));
    objectUrls.add(url);
    thumbCache.set(item.id, url);
    return url;
  }

  function renderGrid() {
    const grid = $("#grid");
    grid.innerHTML = "";
    const list = currentFolder === null ? items : items.filter((i) => i.folder_id === currentFolder);
    $("#empty").hidden = list.length > 0;
    const io = new IntersectionObserver((entries) => {
      entries.forEach(async (en) => {
        if (!en.isIntersecting) return;
        io.unobserve(en.target);
        const it = items.find((x) => x.id === +en.target.dataset.id);
        try {
          en.target.querySelector("img").src = await thumbUrl(it);
        } catch (_) {
          en.target.classList.add("broken");
        }
      });
    }, { rootMargin: "300px" });
    for (const it of list) {
      const card = document.createElement("figure");
      card.className = "vault-card";
      card.dataset.id = it.id;
      card.innerHTML = '<img alt=""><figcaption></figcaption>';
      card.querySelector("figcaption").textContent = `${it.meta.name} · ${it.meta.w || "?"}x${it.meta.h || "?"}`;
      card.addEventListener("click", () => openViewer(it));
      grid.appendChild(card);
      io.observe(card);
    }
  }

  // ---- visualizador (decifra o original sob demanda)
  let viewerUrl = null;
  async function openViewer(it) {
    const v = $("#viewer");
    v.hidden = false;
    $("#viewer-title").textContent = it.meta.name;
    $("#viewer-img").removeAttribute("src");
    $("#viewer-status").textContent = "Baixando e decifrando…";
    const mv = $("#viewer-move");
    mv.innerHTML = '<option value="">(sem pasta)</option>';
    folders.forEach((f) => {
      const o = document.createElement("option");
      o.value = f.id;
      o.textContent = f.name;
      mv.appendChild(o);
    });
    mv.value = it.folder_id ?? "";
    mv.onchange = async () => {
      await api(`/api/vault/items/${it.id}`, { method: "PATCH", json: { folder_id: mv.value ? +mv.value : null } });
      it.folder_id = mv.value ? +mv.value : null;
      renderFolders();
      renderGrid();
    };
    $("#viewer-delete").onclick = async () => {
      if (!confirm(`Apagar "${it.meta.name}" do vault? Não dá para desfazer.`)) return;
      await api(`/api/vault/items/${it.id}`, { method: "DELETE" });
      closeViewer();
      await reload();
    };
    try {
      const r = await api(`/api/vault/items/${it.id}/blob`);
      const plain = await open(masterKey, await r.arrayBuffer(), "file");
      viewerUrl = URL.createObjectURL(new Blob([plain], { type: it.meta.type }));
      $("#viewer-img").src = viewerUrl;
      const dl = $("#viewer-download");
      dl.href = viewerUrl;
      dl.download = it.meta.name;
      $("#viewer-status").textContent = `${it.meta.w}x${it.meta.h} · ${fmtSize(it.meta.size)}`;
    } catch (err) {
      $("#viewer-status").textContent = "Falha ao decifrar: " + err.message;
    }
  }
  function closeViewer() {
    $("#viewer").hidden = true;
    if (viewerUrl) URL.revokeObjectURL(viewerUrl);
    viewerUrl = null;
  }
  $("#viewer-close").addEventListener("click", closeViewer);
  document.addEventListener("keydown", (e) => e.key === "Escape" && closeViewer());

  // ---- pastas
  $("#folder-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = e.target.name.value.trim();
    if (!name) return;
    await api("/api/vault/folders", { method: "POST", json: { name_enc: await sealText(masterKey, { name }, "folder") } });
    e.target.reset();
    await reload();
  });
  $("#delete-folder").addEventListener("click", async () => {
    if (!confirm("Apagar a pasta? Os arquivos voltam para 'Tudo'.")) return;
    await api(`/api/vault/folders/${currentFolder}`, { method: "DELETE" });
    currentFolder = null;
    await reload();
  });

  // ---- upload: miniatura gerada e cifrada localmente
  async function makeThumb(file) {
    const bmp = await createImageBitmap(file);
    const scale = Math.min(1, 480 / bmp.width);
    const c = document.createElement("canvas");
    c.width = Math.round(bmp.width * scale);
    c.height = Math.round(bmp.height * scale);
    c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
    const blob = await new Promise((res) => c.toBlob(res, "image/webp", 0.8));
    const out = { thumb: new Uint8Array(await blob.arrayBuffer()), w: bmp.width, h: bmp.height };
    bmp.close();
    return out;
  }

  $("#upload-input").addEventListener("change", async (e) => {
    const files = [...e.target.files];
    e.target.value = "";
    const log = $("#upload-log");
    const maxBytes = +root.dataset.maxMb * 1048576 - 64;
    for (const file of files) {
      const line = document.createElement("li");
      line.textContent = `${file.name}: cifrando…`;
      log.prepend(line);
      try {
        if (!file.type.startsWith("image/")) throw new Error("só imagens");
        if (file.size > maxBytes) throw new Error(`maior que ${root.dataset.maxMb} MB`);
        const { thumb, w, h } = await makeThumb(file);
        const plain = new Uint8Array(await file.arrayBuffer());
        const fd = new FormData();
        fd.append("blob", new Blob([await seal(masterKey, plain, "file")]), "blob");
        fd.append("thumb", new Blob([await seal(masterKey, thumb, "thumb")]), "thumb");
        fd.append("meta", await sealText(masterKey, { name: file.name, type: file.type, size: file.size, w, h }, "meta"));
        fd.append("folder_id", $("#upload-folder").value);
        line.textContent = `${file.name}: enviando (já cifrado)…`;
        await api("/api/vault/items", { method: "POST", body: fd });
        line.textContent = `${file.name}: ✓ guardado`;
        line.className = "ok";
      } catch (err) {
        line.textContent = `${file.name}: ✗ ${err.message}`;
        line.className = "error";
      }
    }
    await reload();
  });

  init().catch((err) => {
    show("unlock");
    setMsg("#unlock-msg", "Erro ao carregar o vault: " + err.message);
  });
})();
