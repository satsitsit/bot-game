import os, json, hmac, hashlib, random, string
from pathlib import Path
from urllib.parse import parse_qsl
from aiohttp import web, WSMsgType

TOKEN = os.environ["BOT_TOKEN"]  # dari GitHub Secrets, jangan ditulis di kode
# game -> (min pemain, max pemain). Tambah game baru cukup di sini + di static/index.html
LIMITS = {"ludo": (2, 4), "werewolf": (5, 12), "tod": (2, 10),
          "monyet": (3, 3), "gunung": (2, 8), "distrik": (2, 6)}
ROOMS = {}


def verify(raw):
    """Cek initData Telegram supaya cuma Mini App asli yang bisa masuk."""
    try:
        d = dict(parse_qsl(raw, strict_parsing=True))
        got = d.pop("hash")
        s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        ok = hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), got)
        return json.loads(d["user"]) if ok else None
    except Exception:
        return None


async def push(code):
    r = ROOMS[code]
    v = {"t": "room", "code": code, "game": r["game"], "host": r["host"], "started": r["started"],
         "players": [{"id": i, "name": p["name"]} for i, p in r["players"].items()]}
    for p in r["players"].values():
        if not p["ws"].closed:
            await p["ws"].send_json(v)


async def leave(code, uid, ws):
    r = ROOMS.get(code)
    if not r or uid not in r["players"] or r["players"][uid]["ws"] is not ws:
        return
    del r["players"][uid]
    if not r["players"]:
        del ROOMS[code]
        return
    if r["host"] == uid:
        r["host"] = next(iter(r["players"]))
    await push(code)


async def ws_handler(req):
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(req)
    user = code = None
    try:
        async for m in ws:
            if m.type != WSMsgType.TEXT:
                continue
            try:
                msg = json.loads(m.data)
            except ValueError:
                continue
            t = msg.get("t")
            if t == "auth":
                user = verify(msg.get("initData", ""))
                if not user:
                    await ws.send_json({"t": "error", "m": "Buka lewat Telegram ya."})
                    break
                await ws.send_json({"t": "ready", "me": user["id"]})
                continue
            if not user:
                continue
            uid, name = user["id"], user.get("first_name", "Pemain")
            if t == "create" and msg.get("game") in LIMITS:
                await leave(code, uid, ws)
                code = "".join(random.choices(string.ascii_uppercase, k=5))
                ROOMS[code] = {"game": msg["game"], "host": uid, "started": False,
                               "players": {uid: {"name": name, "ws": ws}}}
                await push(code)
            elif t == "join":
                c = str(msg.get("code", "")).upper().strip()
                r = ROOMS.get(c)
                if not r:
                    await ws.send_json({"t": "error", "m": "Kode room tidak ditemukan."})
                elif r["started"] and uid not in r["players"]:
                    await ws.send_json({"t": "error", "m": "Game sudah dimulai."})
                elif uid not in r["players"] and len(r["players"]) >= LIMITS[r["game"]][1]:
                    await ws.send_json({"t": "error", "m": "Room sudah penuh."})
                else:
                    if code and code != c:
                        await leave(code, uid, ws)
                    code = c
                    r["players"][uid] = {"name": name, "ws": ws}
                    await push(c)
            elif t == "start":
                r = ROOMS.get(code)
                if r and r["host"] == uid and not r["started"]:
                    lo, hi = LIMITS[r["game"]]
                    if lo <= len(r["players"]) <= hi:
                        r["started"] = True
                        await push(code)
                    else:
                        await ws.send_json({"t": "error", "m": f"Butuh {lo}–{hi} pemain."})
            elif t == "leave":
                await leave(code, uid, ws)
                code = None
                await ws.send_json({"t": "left"})
    finally:
        if user and code:
            await leave(code, user["id"], ws)
    return ws


async def index(_):
    return web.FileResponse(Path(__file__).parent / "static" / "index.html")


app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/ws", ws_handler)

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=8080)
