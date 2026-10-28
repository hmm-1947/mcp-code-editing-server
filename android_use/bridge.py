import asyncio
import hmac
import json
import socket
import threading
import time
import uuid
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
from starlette.websockets import WebSocket, WebSocketDisconnect

from android_use import fcm, store

WS_PATH = "/android/ws"
PORT = {"value": 8002}


class BridgeError(Exception):
    pass


@dataclass
class Device:
    id: str
    name: str = ""
    model: str = ""
    android: str = ""
    a11y: bool = False
    ws: WebSocket | None = None
    loop: asyncio.AbstractEventLoop | None = None
    connected_at: float = field(default_factory=time.time)


_lock = threading.RLock()
_online: dict[str, Device] = {}
_pending: dict[str, tuple[str, Future]] = {}


def online_devices() -> list[Device]:
    with _lock:
        return list(_online.values())


def _fail_pending(device_id: str, message: str) -> None:
    with _lock:
        stale = [cid for cid, (dev, _) in _pending.items() if dev == device_id]
        futures = [_pending.pop(cid)[1] for cid in stale]
    for fut in futures:
        if not fut.done():
            fut.set_exception(BridgeError(message))


async def ws_endpoint(ws: WebSocket) -> None:
    auth = ws.headers.get("authorization", "")
    if not hmac.compare_digest(auth, f"Bearer {store.token()}"):
        await ws.close(code=4401)
        return
    await ws.accept()
    device: Device | None = None
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), 20))
        if hello.get("t") != "hello" or not hello.get("device_id"):
            await ws.close(code=4400)
            return
        device = Device(
            id=hello["device_id"],
            name=hello.get("name", ""),
            model=hello.get("model", ""),
            android=str(hello.get("android", "")),
            a11y=bool(hello.get("a11y")),
            ws=ws,
            loop=asyncio.get_running_loop(),
        )
        store.remember_device(device.id, name=device.name, model=device.model, fcm_token=hello.get("fcm_token"))
        with _lock:
            previous = _online.get(device.id)
            _online[device.id] = device
        if previous and previous.ws is not None and previous.loop is not None:
            asyncio.run_coroutine_threadsafe(previous.ws.close(code=4000), previous.loop)
        while True:
            message = json.loads(await ws.receive_text())
            kind = message.get("t")
            if kind == "res":
                with _lock:
                    entry = _pending.pop(message.get("id", ""), None)
                if entry and not entry[1].done():
                    entry[1].set_result(message)
            elif kind == "fcm":
                store.remember_device(device.id, fcm_token=message.get("token"))
            elif kind == "status":
                device.a11y = bool(message.get("a11y"))
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, RuntimeError):
        pass
    finally:
        if device is not None:
            with _lock:
                if _online.get(device.id) is device:
                    del _online[device.id]
            _fail_pending(device.id, "the phone disconnected")


def _match_device(query: str, devices) -> list:
    q = query.lower()
    return [d for d in devices if d.id.lower().startswith(q) or q in d.name.lower() or q in d.model.lower()]


def _wake_and_wait(query: str, wait: float) -> Device:
    known = store.devices()
    candidates = {
        did: info for did, info in known.items()
        if info.get("fcm_token") and (not query or query.lower() in (did + info.get("name", "") + info.get("model", "")).lower())
    }
    if not candidates:
        raise BridgeError(
            "No phone is connected. Open the Android Use app on the phone and tap Save and connect "
            "(run android_status for pairing help)."
        )
    if not fcm.configured():
        raise BridgeError("The phone is offline and push wake-up is not configured. Open the Android Use app on the phone.")
    notes = []
    for did, info in candidates.items():
        ok, note = fcm.wake(info["fcm_token"])
        notes.append(f"{info.get('name') or did}: {note}")
    deadline = time.time() + wait
    while time.time() < deadline:
        with _lock:
            for did in candidates:
                if did in _online:
                    return _online[did]
        time.sleep(0.5)
    raise BridgeError(
        "The phone is offline; push wake-up was sent but it did not connect within "
        f"{int(wait)}s ({'; '.join(notes)}). It may be force-stopped, have no network, or the accessibility service is off."
    )


def resolve_device(query: str = "", wake_wait: float = 30.0) -> Device:
    with _lock:
        devices = list(_online.values())
    if query:
        devices = _match_device(query, devices)
    if len(devices) == 1:
        return devices[0]
    if len(devices) > 1:
        names = ", ".join(f"{d.name or d.model} ({d.id[:8]})" for d in devices)
        raise BridgeError(f"Several phones are connected: {names}. Pass device=<name or id prefix>.")
    return _wake_and_wait(query, wake_wait)


def call(op: str, args: dict | None = None, device: str = "", timeout: float = 60.0) -> dict:
    dev = resolve_device(device)
    cid = uuid.uuid4().hex[:10]
    fut: Future = Future()
    with _lock:
        _pending[cid] = (dev.id, fut)
    payload = json.dumps({"t": "cmd", "id": cid, "op": op, "args": args or {}})
    try:
        asyncio.run_coroutine_threadsafe(dev.ws.send_text(payload), dev.loop).result(10)
        return fut.result(timeout)
    except FutureTimeout:
        raise BridgeError(f"The phone did not answer '{op}' within {int(timeout)}s")
    except BridgeError:
        raise
    except Exception as error:
        raise BridgeError(f"Could not reach the phone: {error}")
    finally:
        with _lock:
            _pending.pop(cid, None)


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _ngrok_url() -> str:
    try:
        tunnels = httpx.get("http://127.0.0.1:4040/api/tunnels", timeout=1.5).json().get("tunnels", [])
    except Exception:
        return ""
    for t in tunnels:
        url = t.get("public_url", "")
        if url.startswith("https://"):
            return url
    return ""


def pairing_urls() -> list[str]:
    urls = []
    ngrok = _ngrok_url()
    if ngrok:
        urls.append("wss://" + ngrok[len("https://"):] + WS_PATH)
    urls.append(f"ws://{_lan_ip()}:{PORT['value']}{WS_PATH}")
    return urls


def pairing_code(url: str) -> str:
    return f"androiduse://pair?url={quote(url, safe='')}&token={quote(store.token(), safe='')}"


def start_standalone(host: str, port: int) -> None:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute

    PORT["value"] = port
    app = Starlette(routes=[WebSocketRoute(WS_PATH, ws_endpoint)])
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", ws="websockets-sansio")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True, name="android-bridge").start()
