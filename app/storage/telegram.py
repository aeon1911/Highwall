"""Armazena os wallpapers como documentos num canal privado do Telegram via Bot API.

Fluxo:
  upload  -> sendDocument(chat_id, arquivo)   -> guarda document.file_id + message_id
  leitura -> getFile(file_id) -> file_path     -> baixa /file/bot<TOKEN>/<file_path>
  remoção -> deleteMessage(chat_id, message_id)

O token NUNCA chega ao navegador: as imagens passam pelo proxy /media do próprio site
(com cache em disco), então a URL do Telegram não é exposta.

Limites da API oficial: envio de até 50 MB e download (getFile) de até 20 MB.
Para arquivos maiores, rode o servidor local `telegram-bot-api` e aponte
HIGH_TELEGRAM_API_BASE para ele (limite de 2 GB).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from . import Storage, StoredFile, StorageError


class TelegramStorage(Storage):
    name = "telegram"

    def __init__(
        self,
        token: str,
        chat_id: str,
        api_base: str = "https://api.telegram.org",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not token or not chat_id:
            raise StorageError("Defina HIGH_TELEGRAM_BOT_TOKEN e HIGH_TELEGRAM_CHAT_ID no .env")
        self.token = token
        self.chat_id = chat_id
        self.api_base = api_base.rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), transport=transport)

    def _url(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    async def _call(self, method: str, *, data=None, files=None, retries: int = 4) -> dict:
        for attempt in range(retries):
            try:
                r = await self._client.post(self._url(method), data=data, files=files)
            except httpx.HTTPError as e:
                if attempt == retries - 1:
                    raise StorageError(f"Falha de rede com o Telegram: {type(e).__name__}") from None
                await asyncio.sleep(2**attempt)
                continue
            body = r.json()
            if body.get("ok"):
                return body["result"]
            retry_after = (body.get("parameters") or {}).get("retry_after")
            if r.status_code == 429 and retry_after is not None and attempt < retries - 1:
                await asyncio.sleep(float(retry_after) + 0.5)
                continue
            raise StorageError(f"Telegram {method}: {body.get('description', r.status_code)}")
        raise StorageError(f"Telegram {method}: tentativas esgotadas")

    async def put(self, data: bytes, filename: str, mime: str, caption: str = "") -> StoredFile:
        result = await self._call(
            "sendDocument",
            data={
                "chat_id": self.chat_id,
                "caption": caption[:1024],
                "disable_content_type_detection": "true",
                "disable_notification": "true",
            },
            files={"document": (filename, data, mime)},
        )
        doc = result.get("document")
        if not doc:
            raise StorageError("Telegram não devolveu um documento (arquivo convertido?)")
        return StoredFile(ref=doc["file_id"], message_id=result["message_id"])

    async def fetch(self, ref: str) -> bytes:
        info = await self._call("getFile", data={"file_id": ref})
        file_path = info.get("file_path")
        if not file_path:
            raise StorageError("Arquivo acima do limite de download da Bot API (20 MB na API oficial)")
        # servidor local em modo --local devolve caminho absoluto no disco
        if file_path.startswith("/") and Path(file_path).exists():
            return Path(file_path).read_bytes()
        r = await self._client.get(f"{self.api_base}/file/bot{self.token}/{file_path}")
        if r.status_code != 200:
            raise StorageError(f"Download do Telegram falhou ({r.status_code})")
        return r.content

    async def delete(self, ref: str, message_id: int | None) -> None:
        if message_id is None:
            return
        try:
            await self._call("deleteMessage", data={"chat_id": self.chat_id, "message_id": message_id}, retries=2)
        except StorageError:
            pass  # mensagens com mais de 48h podem não ser apagáveis em alguns tipos de chat

    async def check(self) -> dict:
        """Confere token e acesso ao canal. Usado por `python -m app.cli check-telegram`."""
        me = await self._call("getMe")
        chat = await self._call("getChat", data={"chat_id": self.chat_id})
        return {"bot": me.get("username"), "chat": chat.get("title") or chat.get("id")}
