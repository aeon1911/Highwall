import asyncio
import json

import httpx

from app.storage.telegram import TelegramStorage


def test_telegram_put_fetch_delete():
    stored = {}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        assert "TOKEN" in path  # token só vai para o Telegram
        if path.endswith("/sendDocument"):
            body = request.content
            stored["data"] = body
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 42, "document": {"file_id": "FILE1"}}})
        if path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/file_1.jpg"}})
        if path.endswith("/file/botTOKEN/documents/file_1.jpg"):
            return httpx.Response(200, content=b"IMAGEBYTES")
        if path.endswith("/deleteMessage"):
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(404, json={"ok": False, "description": "nope"})

    st = TelegramStorage("TOKEN", "-100123", transport=httpx.MockTransport(handler))

    async def run():
        f = await st.put(b"IMAGEBYTES", "a.jpg", "image/jpeg", "cap")
        assert (f.ref, f.message_id) == ("FILE1", 42)
        assert await st.fetch("FILE1") == b"IMAGEBYTES"
        await st.delete("FILE1", 42)

    asyncio.run(run())
    assert b"IMAGEBYTES" in stored["data"]
    assert any(c.endswith("/deleteMessage") for c in calls)


def test_telegram_retries_on_429():
    n = {"i": 0}

    def handler(request):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(429, json={"ok": False, "description": "Too Many", "parameters": {"retry_after": 0}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1, "document": {"file_id": "X"}}})

    st = TelegramStorage("TOKEN", "-1", transport=httpx.MockTransport(handler))
    f = asyncio.run(st.put(b"x", "a", "image/jpeg"))
    assert f.ref == "X" and n["i"] == 2
