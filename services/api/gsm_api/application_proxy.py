"""Streaming proxy with an explicit header boundary (also copied into the agent image)."""
import asyncio
import httpx
from fastapi import HTTPException, WebSocket
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
from websockets.asyncio.client import connect

REQUEST_HEADERS = {"accept", "accept-language", "content-type", "if-none-match", "if-modified-since", "range"}
RESPONSE_HEADERS = {"content-type", "content-encoding", "content-disposition", "etag", "last-modified", "location", "content-range", "accept-ranges"}
LIMIT = 100 * 1024 * 1024


async def http_proxy(request, url, extra_headers):
    headers = {k: v for k, v in request.headers.items() if k.lower() in REQUEST_HEADERS}
    headers.update(extra_headers)
    size = 0
    async def body():
        nonlocal size
        async for chunk in request.stream():
            size += len(chunk)
            if size > LIMIT:
                raise HTTPException(413, "文件超过 100 MiB，请使用 SFTP 上传。")
            yield chunk
    if request.headers.get("content-length", "0").isdigit() and int(request.headers.get("content-length", "0")) > LIMIT:
        raise HTTPException(413, "文件超过 100 MiB，请使用 SFTP 上传。")
    client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=8), trust_env=False, follow_redirects=False)
    try:
        upstream = await client.send(client.build_request(request.method, url, headers=headers,
            content=body(), params=request.query_params.multi_items()), stream=True)
    except Exception as exc:
        await client.aclose()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(502, "应用连接中断，请回到容器详情重新打开。") from exc
    async def close():
        await upstream.aclose()
        await client.aclose()
    output_headers = {k: v for k, v in upstream.headers.items() if k.lower() in RESPONSE_HEADERS}
    output_headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "frame-ancestors 'none'",
        "Service-Worker-Allowed": "/__disabled__"})
    return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
        headers=output_headers, background=BackgroundTask(close))


async def websocket_proxy(websocket: WebSocket, url, extra_headers, validate=None):
    offered = websocket.headers.get("sec-websocket-protocol", "").split(",")
    protocols = [p.strip() for p in offered if p.strip() == "v1.kernel.websocket.jupyter.org"]
    tasks = []
    try:
        async with connect(url.replace("http://", "ws://", 1).replace("https://", "wss://", 1),
                additional_headers=extra_headers, subprotocols=protocols or None,
                max_size=16 * 1024 * 1024, proxy=None, open_timeout=8) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)
            async def send():
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    await upstream.send(message.get("bytes") if message.get("bytes") is not None else message.get("text", ""))
            async def receive():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)
            async def revalidate():
                while True:
                    await asyncio.sleep(3)
                    if validate:
                        await asyncio.to_thread(validate)
            tasks = [asyncio.create_task(f()) for f in (send, receive, revalidate)]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    except Exception:
        pass
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await websocket.close(code=1008)
        except Exception:
            pass
