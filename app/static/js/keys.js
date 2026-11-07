// HighWall — criptografia de conta e vault, 100% no navegador (WebCrypto).
//
//   senha ──PBKDF2-SHA256(600k, salt)──▶ segredo ──HKDF──┬─▶ auth_hash  (vai ao servidor, só p/ login)
//                                                         └─▶ KEK        (fica aqui; abre o vault)
//   chave-mestra (32 bytes aleatórios, gerada no cadastro) ──AES-GCM(KEK)──▶ wrapped_key
//   12 palavras BIP39 (128 bits) ──HKDF──┬─▶ chave de recuperação ──AES-GCM──▶ recovery_wrapped_key
//                                        └─▶ recovery_auth (prova ao servidor que você tem as palavras)
//
// A senha, a KEK, a chave-mestra e as palavras nunca saem deste navegador.
"use strict";

(() => {
  const enc = new TextEncoder();
  const dec = new TextDecoder();
  const subtle = crypto.subtle;
  const KEK_STORAGE = "hw_kek";

  // ------------------------------------------------------------ utilidades
  const b64 = (buf) => {
    const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
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
  const csrf = () => document.querySelector('meta[name="csrf-token"]').content;

  async function api(path, { method = "GET", json, body, headers = {} } = {}) {
    const h = { "X-CSRF-Token": csrf(), Accept: "application/json", ...headers };
    if (json !== undefined) {
      h["Content-Type"] = "application/json";
      body = JSON.stringify(json);
    }
    const r = await fetch(path, { method, body, headers: h, credentials: "same-origin" });
    let data = null;
    try { data = await r.json(); } catch (_) {}
    if (!r.ok) {
      const msg = data?.error || (data?.errors || []).join(" ") || data?.detail || `Erro ${r.status}`;
      const err = new Error(msg);
      err.data = data;
      throw err;
    }
    return data;
  }

  // ------------------------------------------------------------ primitivas
  async function hkdf(ikm, salt, info, length = 32) {
    const key = await subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
    const bits = await subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt: enc.encode(salt), info: enc.encode(info) }, key, length * 8);
    return new Uint8Array(bits);
  }
  const importAes = (raw) => subtle.importKey("raw", raw, { name: "AES-GCM" }, false, ["encrypt", "decrypt"]);

  async function seal(key, data, kind) {
    const iv = rand(12);
    const ct = await subtle.encrypt({ name: "AES-GCM", iv, additionalData: AAD(kind) }, key, data);
    return concat(iv, new Uint8Array(ct));
  }
  async function open(key, sealed, kind) {
    const bytes = sealed instanceof Uint8Array ? sealed : new Uint8Array(sealed);
    return subtle.decrypt({ name: "AES-GCM", iv: bytes.subarray(0, 12), additionalData: AAD(kind) }, key, bytes.subarray(12));
  }
  const sealText = async (key, obj, kind) => b64(await seal(key, enc.encode(JSON.stringify(obj)), kind));
  const openText = async (key, s, kind) => JSON.parse(dec.decode(await open(key, unb64(s), kind)));

  /** senha → { authHash (base64, p/ servidor), kekRaw (bytes, fica aqui) } */
  async function deriveFromPassword(password, saltB64, iterations) {
    const base = await subtle.importKey("raw", enc.encode(password.normalize("NFKC")), "PBKDF2", false, ["deriveBits"]);
    const secret = new Uint8Array(
      await subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: unb64(saltB64), iterations }, base, 256)
    );
    const auth = await hkdf(secret, "highwall-auth-v1", "auth");
    const kekRaw = await hkdf(secret, "highwall-kek-v1", "kek");
    secret.fill(0);
    return { authHash: b64(auth), kekRaw };
  }

  /** entropia das 12 palavras → { recKey (CryptoKey), recoveryAuth (base64) } */
  async function deriveFromRecovery(entropy) {
    const keyRaw = await hkdf(entropy, "highwall-recovery-v1", "recovery-key");
    const auth = await hkdf(entropy, "highwall-recovery-v1", "recovery-auth");
    const recKey = await importAes(keyRaw);
    keyRaw.fill(0);
    return { recKey, recoveryAuth: b64(auth) };
  }

  // ------------------------------------------------------------ BIP39 (12 palavras = 128 bits + 4 de checksum)
  async function entropyToWords(entropy) {
    const words = window.BIP39_EN;
    const hash = new Uint8Array(await subtle.digest("SHA-256", entropy));
    let bits = [...entropy].map((b) => b.toString(2).padStart(8, "0")).join("");
    bits += hash[0].toString(2).padStart(8, "0").slice(0, entropy.length / 4);
    return bits.match(/.{11}/g).map((chunk) => words[parseInt(chunk, 2)]);
  }

  async function wordsToEntropy(input) {
    const list = window.BIP39_EN;
    if (!list) throw new Error("Lista de palavras não carregou. Recarregue a página.");
    const words = (Array.isArray(input) ? input : input.trim().toLowerCase().split(/\s+/)).map((w) => w.trim().toLowerCase());
    if (words.length !== 12) throw new Error(`São 12 palavras — você digitou ${words.length}.`);
    const bad = words.filter((w) => !list.includes(w));
    if (bad.length) throw new Error(`Palavra(s) fora da lista: ${bad.join(", ")}`);
    const bits = words.map((w) => list.indexOf(w).toString(2).padStart(11, "0")).join("");
    const entropy = Uint8Array.from(bits.slice(0, 128).match(/.{8}/g), (b) => parseInt(b, 2));
    const hash = new Uint8Array(await subtle.digest("SHA-256", entropy));
    if (hash[0].toString(2).padStart(8, "0").slice(0, 4) !== bits.slice(128)) {
      throw new Error("As palavras não conferem (erro de digitação ou ordem trocada).");
    }
    return entropy;
  }

  /** Gera tudo do zero para uma conta nova (ou conta antiga sendo migrada). */
  async function newAccountKeys(password, iterations) {
    const salt = b64(rand(16));
    const { authHash, kekRaw } = await deriveFromPassword(password, salt, iterations);
    const masterRaw = rand(32);
    const entropy = rand(16);
    const words = await entropyToWords(entropy);
    const { recKey, recoveryAuth } = await deriveFromRecovery(entropy);
    const material = {
      auth_hash: authHash,
      kdf_salt: salt,
      kdf_iterations: iterations,
      wrapped_key: b64(await seal(await importAes(kekRaw), masterRaw, "master")),
      recovery_wrapped_key: b64(await seal(recKey, masterRaw, "master")),
      recovery_auth: recoveryAuth,
    };
    masterRaw.fill(0);
    entropy.fill(0);
    return { material, words, kekRaw };
  }

  /** Re-embrulha a MESMA chave-mestra com uma senha nova (arquivos do vault não mudam). */
  async function rewrapForPassword(masterRaw, newPassword, iterations) {
    const salt = b64(rand(16));
    const { authHash, kekRaw } = await deriveFromPassword(newPassword, salt, iterations);
    const wrapped = b64(await seal(await importAes(kekRaw), masterRaw, "master"));
    return { material: { auth_hash: authHash, kdf_salt: salt, kdf_iterations: iterations, wrapped_key: wrapped }, kekRaw };
  }

  // a KEK fica só nesta aba (sessionStorage some ao fechar a aba) para o vault abrir sem pedir a senha de novo
  const rememberKek = (kekRaw) => sessionStorage.setItem(KEK_STORAGE, b64(kekRaw));
  const recallKek = () => {
    const v = sessionStorage.getItem(KEK_STORAGE);
    return v ? unb64(v) : null;
  };
  const forgetKek = () => sessionStorage.removeItem(KEK_STORAGE);

  function passwordProblem(pw, pw2) {
    if (pw.length < 10) return "A senha precisa ter pelo menos 10 caracteres.";
    if (/^\d+$/.test(pw) || /^\p{L}+$/u.test(pw)) return "Misture letras com números ou símbolos.";
    if (pw2 !== undefined && pw !== pw2) return "As senhas não conferem.";
    return null;
  }

  window.HWCrypto = {
    b64, unb64, rand, api, seal, open, sealText, openText, importAes,
    deriveFromPassword, deriveFromRecovery, entropyToWords, wordsToEntropy,
    newAccountKeys, rewrapForPassword, rememberKek, recallKek, forgetKek, passwordProblem,
  };

  // =================================================================== telas
  const $ = (sel, root = document) => root.querySelector(sel);
  function msg(el, text, kind = "error") {
    if (!el) return;
    el.textContent = text || "";
    el.className = "msg " + kind;
    el.hidden = !text;
  }
  function busy(form, on, label) {
    form.querySelectorAll("button, input, select").forEach((el) => (el.disabled = on));
    const btn = form.querySelector("button.accent");
    if (btn && label) {
      btn.dataset.label ??= btn.textContent;
      btn.textContent = on ? label : btn.dataset.label;
    }
  }
  const secureOk = () => window.isSecureContext && !!crypto.subtle;

  /**
   * Mostra as 12 palavras e depois pede 5 delas, sorteadas. Resolve quando a pessoa acertar.
   * Usa o bloco do template _phrase.html.
   */
  function phraseFlow(box, words) {
    fillDatalist(); // a lista BIP39 carrega depois deste script, então só monta aqui
    return new Promise((resolve) => {
      const show = $("[data-phrase=show]", box);
      const confirmStep = $("[data-phrase=confirm]", box);
      const grid = $(".phrase-grid", box);
      grid.innerHTML = "";
      words.forEach((w, i) => {
        const li = document.createElement("li");
        li.innerHTML = "<b></b><span></span>";
        li.querySelector("b").textContent = i + 1;
        li.querySelector("span").textContent = w;
        grid.appendChild(li);
      });
      box.hidden = false;
      show.hidden = false;
      confirmStep.hidden = true;
      const ack = $("[data-phrase=ack]", box);
      ack.checked = false;

      $("[data-phrase=next]", box).onclick = () => {
        if (!ack.checked) return alert("Confirme que anotou as 12 palavras.");
        // sorteia 5 posições diferentes
        const idx = [...Array(12).keys()];
        for (let i = idx.length - 1; i > 0; i--) { // Fisher-Yates com aleatoriedade criptográfica
          const j = crypto.getRandomValues(new Uint32Array(1))[0] % (i + 1);
          [idx[i], idx[j]] = [idx[j], idx[i]];
        }
        const pos = idx.slice(0, 5).sort((a, b) => a - b);
        const inputs = $(".phrase-check", box);
        inputs.innerHTML = "";
        pos.forEach((p) => {
          const lbl = document.createElement("label");
          lbl.innerHTML = `<span>Palavra nº ${p + 1}</span><input list="bip39-list" autocomplete="off" autocapitalize="none" spellcheck="false">`;
          lbl.querySelector("input").dataset.pos = p;
          inputs.appendChild(lbl);
        });
        grid.innerHTML = ""; // esconde as palavras durante a confirmação
        show.hidden = true;
        confirmStep.hidden = false;
        inputs.querySelector("input").focus();
      };
      $("[data-phrase=back]", box).onclick = () => phraseFlowRestore();
      const phraseFlowRestore = () => {
        words.forEach((w, i) => {
          const li = document.createElement("li");
          li.innerHTML = "<b></b><span></span>";
          li.querySelector("b").textContent = i + 1;
          li.querySelector("span").textContent = w;
          grid.appendChild(li);
        });
        show.hidden = false;
        confirmStep.hidden = true;
      };
      $("[data-phrase=verify]", box).onclick = () => {
        const fields = [...box.querySelectorAll(".phrase-check input")];
        const wrong = fields.filter((f) => f.value.trim().toLowerCase() !== words[+f.dataset.pos]);
        fields.forEach((f) => f.classList.toggle("invalid", wrong.includes(f)));
        if (wrong.length) {
          msg($("[data-phrase=msg]", box), `${wrong.length} palavra(s) não conferem. Confira sua anotação.`);
          return;
        }
        msg($("[data-phrase=msg]", box), "");
        box.querySelectorAll(".phrase-check input").forEach((f) => (f.value = ""));
        resolve();
      };
    });
  }

  function fillDatalist() {
    if (document.getElementById("bip39-list") || !window.BIP39_EN) return;
    const dl = document.createElement("datalist");
    dl.id = "bip39-list";
    dl.innerHTML = window.BIP39_EN.map((w) => `<option value="${w}">`).join("");
    document.body.appendChild(dl);
  }

  // ------------------------------------------------------------ cadastro
  const reg = document.getElementById("register-form");
  if (reg) {
    const out = $("#register-msg");
    const iterations = +reg.dataset.iterations;
    reg.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!secureOk()) return msg(out, "O cadastro precisa de HTTPS (ou localhost) para gerar suas chaves.");
      const f = reg.elements;
      const data = {
        username: f.username.value.trim(), email: f.email.value.trim(), birthdate: f.birthdate.value,
        accept_terms: f.accept_terms.checked,
      };
      const problem = passwordProblem(f.password.value, f.password2.value);
      if (problem) return msg(out, problem);
      busy(reg, true, "Verificando…");
      try {
        const chk = await api("/api/register/check", { method: "POST", json: data });
        if (!chk.ok) throw new Error(chk.errors.join(" "));
        msg(out, "Gerando suas chaves no navegador…", "info");
        busy(reg, true, "Gerando chaves…");
        const { material, words, kekRaw } = await newAccountKeys(f.password.value, iterations);
        f.password.value = f.password2.value = "";
        msg(out, "");
        reg.hidden = true;
        await phraseFlow($("#phrase-box"), words);
        words.fill("");
        const done = $("#phrase-box [data-phrase=verify]");
        const submit = async () => {
          done.disabled = true;
          done.textContent = "Criando conta…";
          try {
            const res = await api("/register", { method: "POST", json: { ...data, ...material } });
            rememberKek(kekRaw);
            location.href = res.redirect;
          } catch (err) {
            done.disabled = false;
            if (err.data?.errors) {
              // ex.: alguém pegou o mesmo usuário nesse meio tempo → volta ao formulário (novas palavras serão geradas)
              $("#phrase-box").hidden = true;
              reg.hidden = false;
              msg(out, err.message + " As palavras anteriores foram descartadas.");
            } else {
              done.textContent = "Tentar de novo";
              done.onclick = submit;
              msg($("#phrase-box [data-phrase=msg]"), err.message);
            }
          }
        };
        await submit();
      } catch (err) {
        msg(out, err.message);
      } finally {
        busy(reg, false);
      }
    });
  }

  // ------------------------------------------------------------ login
  const login = document.getElementById("login-form");
  if (login) {
    const out = $("#login-msg");
    login.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!secureOk()) return msg(out, "O login precisa de HTTPS (ou localhost).");
      const f = login.elements;
      busy(login, true, "Entrando…");
      try {
        const pre = await api(`/api/prelogin?login=${encodeURIComponent(f.login.value.trim())}`);
        const body = { login: f.login.value.trim(), next: f.next.value };
        let kekRaw = null;
        if (pre.scheme === "legacy") {
          body.password = f.password.value; // conta antiga: última vez que a senha vai ao servidor
        } else {
          const d = await deriveFromPassword(f.password.value, pre.salt, pre.iterations);
          body.auth_hash = d.authHash;
          kekRaw = d.kekRaw;
        }
        const res = await api("/login", { method: "POST", json: body });
        if (kekRaw) rememberKek(kekRaw);
        location.href = res.redirect;
      } catch (err) {
        msg(out, err.message);
        busy(login, false);
      }
    });
  }

  // ------------------------------------------------------------ conta antiga → gerar 12 palavras
  const setup = document.getElementById("setup-key-form");
  if (setup) {
    const out = $("#setup-msg");
    setup.addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = setup.elements;
      busy(setup, true, "Verificando…");
      try {
        const ok = await api("/api/account/verify-legacy", { method: "POST", json: { password: f.password.value } });
        if (!ok.ok) throw new Error("Senha incorreta.");
        busy(setup, true, "Gerando chaves…");
        const { material, words, kekRaw } = await newAccountKeys(f.password.value, +setup.dataset.iterations);
        const password = f.password.value;
        f.password.value = "";
        setup.hidden = true;
        await phraseFlow($("#phrase-box"), words);
        const res = await api("/api/account/setup-key", { method: "POST", json: { password, ...material } });
        rememberKek(kekRaw);
        location.href = res.redirect;
      } catch (err) {
        setup.hidden = false;
        $("#phrase-box").hidden = true;
        msg(out, err.message);
      } finally {
        busy(setup, false);
      }
    });
  }

  // ------------------------------------------------------------ recuperar com as 12 palavras
  const rec = document.getElementById("recover-form");
  if (rec) {
    const out = $("#recover-msg");
    rec.addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = rec.elements;
      const problem = passwordProblem(f.password.value, f.password2.value);
      if (problem) return msg(out, problem);
      busy(rec, true, "Recuperando…");
      try {
        const entropy = await wordsToEntropy(f.words.value);
        const { recKey, recoveryAuth } = await deriveFromRecovery(entropy);
        entropy.fill(0);
        const start = await api("/api/recover/start", { method: "POST", json: { login: f.login.value.trim(), recovery_auth: recoveryAuth } });
        const masterRaw = new Uint8Array(await open(recKey, unb64(start.recovery_wrapped_key), "master"));
        const { material, kekRaw } = await rewrapForPassword(masterRaw, f.password.value, +rec.dataset.iterations);
        masterRaw.fill(0);
        const res = await api("/api/recover/finish", { method: "POST", json: material });
        rememberKek(kekRaw);
        location.href = res.redirect;
      } catch (err) {
        msg(out, err.message);
        busy(rec, false);
      }
    });
  }

  // ------------------------------------------------------------ trocar senha (configurações)
  const pwf = document.getElementById("password-form");
  if (pwf) {
    const out = $("#password-msg");
    pwf.addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = pwf.elements;
      const problem = passwordProblem(f.new.value, f.new2.value);
      if (problem) return msg(out, problem);
      busy(pwf, true, "Trocando…");
      try {
        const info = await api("/api/vault/key");
        if (!info.exists) throw new Error("Gere sua frase de recuperação primeiro.");
        const old = await deriveFromPassword(f.current.value, info.salt, info.iterations);
        let masterRaw;
        try {
          masterRaw = new Uint8Array(await open(await importAes(old.kekRaw), unb64(info.wrapped_key), "master"));
        } catch (_) {
          throw new Error("Senha atual incorreta.");
        }
        const { material, kekRaw } = await rewrapForPassword(masterRaw, f.new.value, +pwf.dataset.iterations);
        masterRaw.fill(0);
        await api("/api/account/password", { method: "POST", json: { current_auth: old.authHash, ...material } });
        rememberKek(kekRaw);
        pwf.reset();
        msg(out, "Senha alterada. Suas 12 palavras continuam valendo.", "ok");
      } catch (err) {
        msg(out, err.message);
      } finally {
        busy(pwf, false);
      }
    });
  }

  // ------------------------------------------------------------ sair: apaga a KEK desta aba
  document.addEventListener("submit", (e) => {
    if (e.target.getAttribute("action") === "/logout") forgetKek();
  });
})();
