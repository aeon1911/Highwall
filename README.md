# HighWall

Site de wallpapers sensuais **para maiores de 18 anos**, só em **2K ou mais**, com as imagens hospedadas no **Telegram** (Bot API).

Stack: Python 3 · FastAPI · SQLAlchemy (SQLite) · Jinja2 · Pillow · JS puro (WebCrypto).

## Rodar

```bash
./run.sh            # cria .venv, instala dependências e sobe em http://127.0.0.1:8000
```

O **primeiro usuário cadastrado vira administrador**. Em desenvolvimento o `.env` usa `HIGH_STORAGE_BACKEND=local`
(arquivos em `data/files`). Testes: `.venv/bin/python -m pytest`.

## Telegram como armazenamento

1. Crie um bot com o @BotFather e copie o token.
2. Crie um **canal privado**, adicione o bot como administrador e pegue o ID (`-100…`).
3. No `.env`: `HIGH_STORAGE_BACKEND=telegram`, `HIGH_TELEGRAM_BOT_TOKEN`, `HIGH_TELEGRAM_CHAT_ID`.
4. `python -m app.cli check-telegram` confirma o acesso.

Cada wallpaper vira 2 documentos no canal (original + miniatura WEBP). O banco guarda só o `file_id` e o `message_id`.
As imagens passam pelo proxy `/media/…` com cache LRU em disco (`HIGH_CACHE_MAX_MB`), então **o token nunca chega ao navegador**.

**Limite importante:** na API oficial o download (`getFile`) só funciona até **20 MB**, por isso `HIGH_MAX_UPLOAD_MB=20`.
Para aceitar PNGs 8K maiores, rode o servidor local [`telegram-bot-api`](https://github.com/tdlib/telegram-bot-api)
(limite de 2 GB), aponte `HIGH_TELEGRAM_API_BASE` para ele e aumente o limite.

## Funcionalidades

- **Idade:** verificação de 18+ antes de qualquer página, data de nascimento obrigatória no cadastro, conteúdo
  *Explícito* (nsfw) só para quem está logado, e o cabeçalho RTA para filtros parentais.
- **Contas:** senhas com Argon2, sessão assinada, CSRF em todos os POSTs, limite de tentativas de login, cadastro e upload, e papéis user/moderator/admin.
- **Upload 2K+:** lado maior ≥ 2560 e lado menor ≥ 1440 (paisagem ou retrato), checado no navegador e no servidor.
  Remove EXIF/GPS sem recomprimir, bloqueia duplicata exata (SHA-256) e quase-duplicata (dHash),
  aceita vários arquivos por vez e manda envios de usuários comuns para moderação.
- **Categorias com cores:** cada categoria tem uma paleta curada e também mostra as cores reais mais presentes nos wallpapers dela.
- **Cores:** cada wallpaper é mapeado numa paleta fixa de 34 tons (inclui tons de pele e neon) e dá para buscar por cor.
- **Tags (mais completas que as do wallhaven):** categorias hierárquicas, **aliases**, **implicações transitivas**
  (`lingerie preta` ⇒ `lingerie`) com detecção de ciclo, mesclagem de tags, pureza por tag e tags relacionadas.
- **Personagens e séries:** `Nome (Série)` cria os dois e aceita aliases.
- **Busca:** `+tag -tag ~ou "duas palavras" tag:slug char:slug series:slug cat:slug @usuario color:rosa
  ratio:16x9 res: atleast: orientation: purity: like:ID id:ID`, mais filtros de pureza, categoria, proporção,
  resolução mínima e cor. Ordenação por data, relevância, views, favoritos, toplist (1d…1y) ou aleatória com seed.
  O mesmo também existe em JSON em `/api/search`.
- **Galeria privada:** pastas e subpastas só do dono para salvar wallpapers do site.
- **Vault criptografado (E2E):** veja abaixo.
- **Moderação:** fila de aprovação, denúncias (uma denúncia de "menor de idade" tira o wallpaper do ar na hora), editor de tags e de
  categorias/cores, e gestão de usuários.

## Contas e vault: criptografia no navegador (conhecimento zero)

A senha **nunca sai do navegador** (modelo do Bitwarden). Tudo é gerado no **cadastro**:

```
senha ──PBKDF2-SHA256 (600 mil it., salt)──▶ segredo ──HKDF──┬─▶ auth_hash → servidor guarda argon2(auth_hash)
                                                              └─▶ KEK       → fica no navegador
chave-mestra do vault (256 bits aleatórios) ──AES-GCM(KEK)──▶ wrapped_key            → servidor
12 palavras BIP39 (128 bits) ──HKDF──┬─▶ chave de recuperação ──AES-GCM──▶ recovery_wrapped_key → servidor
                                     └─▶ recovery_auth → servidor guarda argon2(recovery_auth)
arquivos, miniaturas, nomes e pastas do vault ──AES-GCM(chave-mestra)──▶ servidor / Telegram
```

- **Cadastro:** o navegador gera as chaves e mostra as **12 palavras** (lista oficial BIP39 em inglês, com checksum,
  como numa carteira de bitcoin). A pessoa marca que anotou e depois digita **5 palavras sorteadas**. A conta só é
  criada se ela acertar.
- **Login:** `/api/prelogin` devolve o salt. O navegador deriva o `auth_hash` e só ele é enviado. Logins inexistentes
  recebem um salt falso e estável, para não revelar quem tem conta.
- **Vault:** abre com a mesma senha de login. A KEK fica em `sessionStorage`, só naquela aba, e é apagada ao sair,
  ao trancar ou depois de 15 minutos sem uso.
- **Formatar o PC ou trocar de aparelho não faz perder nada:** a chave não fica presa ao dispositivo. Basta a senha
  ou as 12 palavras.
- **Esqueci a senha:** em `/recover`, as 12 palavras (checadas no navegador; o checksum pega erros de digitação ou de
  ordem) abrem a chave-mestra e definem uma senha nova. O vault continua intacto e as palavras continuam valendo.
- **Trocar senha:** acontece no navegador e re-embrulha a mesma chave-mestra, então nenhum arquivo precisa ser recifrado.
- **Contas antigas** (criadas antes deste sistema) são levadas a `/setup-key` no login para gerar as palavras.
- **Sem a senha e sem as 12 palavras, nada é recuperável**, nem pela administração. Isso é proposital.
- Login e cadastro exigem JavaScript e HTTPS em produção (WebCrypto só funciona em contexto seguro; localhost é aceito).
- O servidor não vê a senha, então a regra de força da senha é aplicada só no navegador.

## Produção

- `HIGH_DEBUG=false` (cookie só via HTTPS) e um `HIGH_SECRET_KEY` longo e aleatório.
- Ponha atrás de um proxy HTTPS (Caddy/Nginx) e rode `uvicorn app.main:app --workers 1`.
  Os rate limits ficam em memória; para mais de um worker, troque por Redis.
- Para muitos usuários, troque `HIGH_DATABASE_URL` para PostgreSQL.

## CLI

```bash
python -m app.cli check-telegram
python -m app.cli set-role USUARIO moderator
python -m app.cli recount
```
