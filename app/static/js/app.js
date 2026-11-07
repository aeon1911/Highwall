"use strict";

const CSRF = () => document.querySelector('meta[name="csrf-token"]').content;

async function post(url, data, accept = "application/json") {
  const body = data instanceof FormData ? data : new URLSearchParams(data);
  const r = await fetch(url, { method: "POST", body, headers: { "X-CSRF-Token": CSRF(), Accept: accept }, credentials: "same-origin" });
  if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname); throw new Error("login"); }
  return r;
}

// ------------------------------------------------------------ confirmações
document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm || e.submitter?.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

// ------------------------------------------------------------ favoritos
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-fav]");
  if (!btn) return;
  e.preventDefault();
  const r = await post(`/w/${btn.dataset.fav}/favorite`, {});
  if (!r.ok) return;
  const j = await r.json();
  btn.classList.toggle("on", j.favorited);
  const span = btn.querySelector("span");
  if (span) span.textContent = j.count;
});

// ------------------------------------------------------------ salvar na galeria (pastas)
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-save]");
  if (!btn) return;
  const panel = document.getElementById("save-panel");
  const wid = btn.dataset.save;
  if (!panel.hidden) { panel.hidden = true; return; }
  const r = await fetch(`/gallery/api/folders-for/${wid}`, { credentials: "same-origin" });
  const folders = await r.json();
  panel.innerHTML = "";
  const title = document.createElement("strong");
  title.textContent = "Salvar na galeria";
  panel.appendChild(title);
  const list = document.createElement("div");
  list.className = "folder-checks";
  folders.forEach((f) => {
    const lbl = document.createElement("label");
    lbl.style.paddingLeft = f.depth * 14 + "px";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = f.saved;
    cb.addEventListener("change", async () => {
      await post("/gallery/save", { wallpaper_id: wid, collection_id: f.id, action: cb.checked ? "add" : "remove" });
    });
    lbl.append(cb, " 📁 " + f.name);
    list.appendChild(lbl);
  });
  if (!folders.length) list.textContent = "Você ainda não tem pastas.";
  panel.appendChild(list);
  const form = document.createElement("form");
  form.className = "row";
  form.innerHTML = '<input name="new_folder" placeholder="Nova pasta" maxlength="64" required><button class="btn small">Criar e salvar</button>';
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    await post("/gallery/save", { wallpaper_id: wid, new_folder: form.new_folder.value });
    panel.hidden = true;
    btn.click();
  });
  panel.appendChild(form);
  panel.hidden = false;
});

// ------------------------------------------------------------ autocomplete de tags / personagens
function setupAutocomplete(input) {
  const kind = input.dataset.autocomplete;
  const searchMode = input.dataset.mode === "search";
  const single = !!input.dataset.single;
  const box = document.createElement("ul");
  box.className = "ac-box";
  box.hidden = true;
  input.parentElement.style.position = "relative";
  input.insertAdjacentElement("afterend", box);
  let timer = null, active = -1, results = [];

  const currentToken = () => {
    const v = input.value;
    if (single) return { start: 0, token: v };
    if (searchMode) {
      const start = v.lastIndexOf(" ") + 1;
      let tok = v.slice(start);
      const prefix = /^[+\-~]/.test(tok) ? tok[0] : "";
      return { start: start + prefix.length, token: tok.slice(prefix.length) };
    }
    const start = v.lastIndexOf(",") + 1;
    return { start, token: v.slice(start).trim() };
  };

  const pick = (item) => {
    const { start } = currentToken();
    const v = input.value.slice(0, start);
    let val = kind === "characters" ? item.label : item.name;
    if (searchMode) val = (kind === "characters" ? "char:" + item.slug : val.includes(" ") ? `"${val}"` : val) + " ";
    else if (!single) val = (start ? " " : "") + val + ", ";
    input.value = v + val;
    box.hidden = true;
    input.focus();
  };

  const render = () => {
    box.innerHTML = "";
    results.forEach((it, i) => {
      const li = document.createElement("li");
      li.className = (i === active ? "active " : "") + "p-" + (it.purity || "sfw");
      li.textContent = kind === "characters" ? it.label : it.name;
      const small = document.createElement("small");
      small.textContent = (it.alias ? ` ← ${it.alias}` : "") + (it.category ? ` · ${it.category}` : "") + ` · ${it.count}`;
      li.appendChild(small);
      li.addEventListener("mousedown", (e) => { e.preventDefault(); pick(it); });
      box.appendChild(li);
    });
    box.hidden = !results.length;
  };

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const { token } = currentToken();
    if (token.length < 1 || token.includes(":") || token.startsWith("@")) { box.hidden = true; return; }
    timer = setTimeout(async () => {
      const url = kind === "characters" ? "/api/characters/autocomplete" : "/api/tags/autocomplete";
      const r = await fetch(`${url}?q=${encodeURIComponent(token)}`, { credentials: "same-origin" });
      results = r.ok ? await r.json() : [];
      active = -1;
      render();
    }, 150);
  });
  input.addEventListener("keydown", (e) => {
    if (box.hidden) return;
    if (e.key === "ArrowDown") { active = Math.min(results.length - 1, active + 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active = Math.max(0, active - 1); render(); e.preventDefault(); }
    else if ((e.key === "Enter" || e.key === "Tab") && active >= 0) { pick(results[active]); e.preventDefault(); }
    else if (e.key === "Escape") box.hidden = true;
  });
  input.addEventListener("blur", () => setTimeout(() => (box.hidden = true), 150));
}
document.querySelectorAll("[data-autocomplete]").forEach(setupAutocomplete);

// ------------------------------------------------------------ upload múltiplo com checagem de resolução
(() => {
  const form = document.getElementById("upload-form");
  if (!form) return;
  const input = document.getElementById("files");
  const queue = document.getElementById("queue");
  const drop = document.getElementById("dropzone");
  const maxBytes = +form.dataset.maxMb * 1048576;
  const minLong = +form.dataset.minLong, minShort = +form.dataset.minShort;
  let files = [];

  const addFiles = async (list) => {
    for (const file of list) {
      const li = document.createElement("li");
      const entry = { file, li, ok: true };
      li.textContent = `${file.name} — verificando…`;
      queue.appendChild(li);
      files.push(entry);
      try {
        if (file.size > maxBytes) throw new Error(`maior que ${form.dataset.maxMb} MB`);
        const bmp = await createImageBitmap(file);
        const [w, h] = [bmp.width, bmp.height];
        bmp.close();
        if (Math.max(w, h) < minLong || Math.min(w, h) < minShort) throw new Error(`${w}x${h} abaixo de 2K`);
        li.textContent = `${file.name} — ${w}x${h} ✓`;
      } catch (err) {
        entry.ok = false;
        li.textContent = `${file.name} — ✗ ${err.message}`;
        li.className = "error";
      }
    }
  };
  input.addEventListener("change", () => { addFiles([...input.files]); input.value = ""; });
  ["dragover", "dragenter"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (e) => { e.preventDefault(); addFiles([...e.dataTransfer.files]); });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const todo = files.filter((f) => f.ok && !f.done);
    if (!todo.length) return alert("Adicione pelo menos uma imagem válida (2K ou maior).");
    const btn = document.getElementById("upload-btn");
    btn.disabled = true;
    for (const entry of todo) {
      entry.li.textContent = `${entry.file.name} — enviando…`;
      const fd = new FormData(form);
      fd.append("file", entry.file);
      try {
        const r = await post("/upload", fd);
        const j = await r.json();
        if (!j.ok) throw new Error(j.error);
        entry.done = true;
        entry.li.innerHTML = "";
        const a = document.createElement("a");
        a.href = j.url;
        a.textContent = `${entry.file.name} — ✓ #${j.id} (${j.status === "approved" ? "publicado" : "aguardando moderação"})`;
        entry.li.appendChild(a);
        entry.li.className = "ok";
      } catch (err) {
        entry.li.textContent = `${entry.file.name} — ✗ ${err.message}`;
        entry.li.className = "error";
      }
    }
    btn.disabled = false;
  });
})();
