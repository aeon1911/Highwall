// Vault pessoal com criptografia de ponta a ponta. As primitivas ficam em keys.js (window.HWCrypto).
//
//  senha de login ──PBKDF2 + HKDF (keys.js)──▶ KEK ──abre──▶ chave-mestra (gerada no cadastro)
//  arquivo / miniatura / metadados / nome de pasta ──AES-GCM(chave-mestra)──▶ servidor/Telegram
//
// Formato de todo ciphertext: IV (12 bytes) || AES-GCM(ciphertext + tag 16 bytes).
// Um AAD diferente por tipo de dado impede trocar a miniatura de um item pelo arquivo de outro.
"use strict";

(() => {
  const root = document.getElementById("vault");
  if (!root) return;

  const C = window.HWCrypto;
  const { unb64, seal, open, sealText, openText, importAes } = C;
  const AUTO_LOCK_MS = 15 * 60 * 1000;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const $ = (sel) => root.querySelector(sel);

  let masterKey = null; // CryptoKey não-extraível, só em memória
  let keyInfo = null;
  let folders = []; // {id, name}
  let items = []; // {id, folder_id, meta:{...}, size}
  let currentFolder = null;
  const objectUrls = new Set();
  let lockTimer = null;

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

  function show(id) {
    root.querySelectorAll("[data-screen]").forEach((el) => (el.hidden = el.dataset.screen !== id));
  }
  function setMsg(sel, text, kind = "error") {
    const el = $(sel);
    el.textContent = text;
    el.className = "msg " + kind;
    el.hidden = !text;
  }

  /** KEK (bytes) → chave-mestra. Lança erro se a KEK não abrir (senha errada). */
  async function unlockWith(kekRaw) {
    const raw = new Uint8Array(await open(await importAes(kekRaw), unb64(keyInfo.wrapped_key), "master"));
    masterKey = await importAes(raw);
    raw.fill(0);
  }

  async function init() {
    if (!window.isSecureContext || !crypto.subtle) return show("insecure");
    keyInfo = await (await api("/api/vault/key")).json();
    if (!keyInfo.exists) return (location.href = "/setup-key");
    const kek = C.recallKek(); // já derivada no login, nesta aba
    if (kek) {
      try {
        await unlockWith(kek);
        return enterVault();
      } catch (_) {
        C.forgetKek(); // senha mudou em outro aparelho
      }
    }
    show("unlock");
  }

  $("#unlock-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    f.querySelectorAll("button, input").forEach((el) => (el.disabled = true));
    setMsg("#unlock-msg", "Derivando chave…", "info");
    try {
      const { kekRaw } = await C.deriveFromPassword(f.pass.value, keyInfo.salt, keyInfo.iterations);
      try {
        await unlockWith(kekRaw);
      } catch (_) {
        throw new Error("Senha incorreta.");
      }
      C.rememberKek(kekRaw);
      f.reset();
      setMsg("#unlock-msg", "");
      enterVault();
    } catch (err) {
      setMsg("#unlock-msg", err.message);
    } finally {
      f.querySelectorAll("button, input").forEach((el) => (el.disabled = false));
    }
  });

  // ------------------------------------------------------------ vault aberto
  function lock() {
    masterKey = null;
    C.forgetKek(); // trancar exige a senha de novo
    thumbCache.clear();
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
