import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent))
import asyncio, json, sys
import websockets
from fastmcp import Client
from android_use import store

async def phone():
    async with websockets.connect("ws://127.0.0.1:8099/android/ws", additional_headers={"Authorization": "Bearer " + store.token()}) as ws:
        await ws.send(json.dumps({"t": "hello", "device_id": "test-dev", "name": "Fake", "model": "X", "android": "14", "a11y": True}))
        async for raw in ws:
            m = json.loads(raw)
            await ws.send(json.dumps({"t": "res", "id": m["id"], "ok": True, "text": f"got {m['op']} {json.dumps(m['args'])}"}))

async def main():
    task = asyncio.create_task(phone())
    await asyncio.sleep(1)
    async with Client("http://127.0.0.1:8099/mcp") as c:
        print([t.name for t in await c.list_tools()])
        for cmd in ("android tap 3 --screen", "android screen --shot", "android type text=hi submit=1", "android swipe direction=up", "android status"):
            print((await c.call_tool("run", {"command": cmd})).content[0].text)
    task.cancel()
    try:
        async with websockets.connect("ws://127.0.0.1:8099/android/ws", additional_headers={"Authorization": "Bearer bad"}) as ws:
            await ws.recv()
    except Exception as e:
        print("bad token:", type(e).__name__)

asyncio.run(main())
