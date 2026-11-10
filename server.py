import os, json, hmac, hashlib, random, time, asyncio
from pathlib import Path
from urllib.parse import parse_qsl
from aiohttp import web, WSMsgType, ClientSession, ClientTimeout

TOKEN = os.environ["BOT_TOKEN"]  # dari GitHub Secrets, jangan ditulis langsung di kode
LIMITS = {"ludo": (2, 4), "werewolf": (5, 12), "tod": (2, 10),
          "monyet": (3, 3), "gunung": (2, 8), "distrik": (2, 6)}  # game -> (min, max) pemain
CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # tanpa I, L, O, 0, 1 supaya gampang dibaca
MAX_AUTH_AGE = 86400  # initData lebih dari 24 jam dianggap basi
GRACE = 40  # detik sebelum pemain yang putus koneksi dikeluarkan dari room
ROOMS, TASKS = {}, set()
BOT = {"username": ""}
STATIC = Path(__file__).parent / "static"


def verify(raw):
    """Cek tanda tangan initData dari Telegram. Hanya user asli dari Telegram yang lolos."""
    try:
        d = dict(parse_qsl(raw, keep_blank_values=True, strict_parsing=True))
        got = d.pop("hash")
        if time.time() - int(d.get("auth_date", 0)) > MAX_AUTH_AGE:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return json.loads(d["user"]) if hmac.compare_digest(calc, got) else None
    except Exception:
        return None


def who(u):
    return str(u.get("first_name") or "").strip()[:16] or "Pemain"


def spawn(coro):
    t = asyncio.create_task(coro)
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)


async def send(ws, obj):
    if ws is None or ws.closed:
        return
    try:
        await ws.send_json(obj)
    except Exception:
        pass


async def push(code):
    r = ROOMS.get(code)
    if not r:
        return
    v = {"t": "room", "code": code, "game": r["game"], "host": r["host"], "started": r["started"],
         "players": [{"id": i, "name": p["name"], "on": p["ws"] is not None}
                     for i, p in r["players"].items()]}
    for p in list(r["players"].values()):
        await send(p["ws"], v)


async def remove(code, uid):
    r = ROOMS.get(code)
    if not r or uid not in r["players"]:
        return
    del r["players"][uid]
    if not r["players"]:
        del ROOMS[code]
        return
    if r["host"] == uid:
        online = [i for i, p in r["players"].items() if p["ws"] is not None]
        r["host"] = online[0] if online else next(iter(r["players"]))
    await push(code)


async def expire(code, uid):
    await asyncio.sleep(GRACE)
    r = ROOMS.get(code)
    if r and uid in r["players"] and r["players"][uid]["ws"] is None:
        await remove(code, uid)


def new_code():
    while True:
        c = "".join(random.choices(CODE_CHARS, k=5))
        if c not in ROOMS:
            return c


async def ws_handler(req):
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=4096)
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
            if not isinstance(msg, dict):
                continue
            t = msg.get("t")
            if t == "auth":
                user = verify(str(msg.get("initData", "")))
                if not user:
                    await send(ws, {"t": "fatal", "m": "Buka Ngumpul lewat bot Telegram ya."})
                    break
                await send(ws, {"t": "ready", "me": user["id"], "bot": BOT["username"]})
                continue
            if not user:
                continue
            uid, name = user["id"], who(user)
            if t == "create" and msg.get("game") in LIMITS:
                if code:
                    await remove(code, uid)
                code = new_code()
                ROOMS[code] = {"game": msg["game"], "host": uid, "started": False,
                               "players": {uid: {"name": name, "ws": ws}}}
                await push(code)
            elif t == "join":
                c = str(msg.get("code", "")).upper().strip()
                r = ROOMS.get(c)
                if not r:
                    await send(ws, {"t": "error", "c": "notfound", "m": "Kode room tidak ditemukan."})
                elif uid not in r["players"] and r["started"]:
                    await send(ws, {"t": "error", "c": "started", "m": "Game sudah dimulai."})
                elif uid not in r["players"] and len(r["players"]) >= LIMITS[r["game"]][1]:
                    await send(ws, {"t": "error", "c": "full", "m": "Room sudah penuh."})
                else:
                    if code and code != c:
                        await remove(code, uid)
                    code = c
                    r["players"][uid] = {"name": name, "ws": ws}
                    await push(c)
            elif t == "start" and code in ROOMS:
                r = ROOMS[code]
                lo, hi = LIMITS[r["game"]]
                if r["host"] == uid and not r["started"]:
                    if lo <= len(r["players"]) <= hi:
                        r["started"] = True
                        await push(code)
                    else:
                        await send(ws, {"t": "error", "c": "count", "m": f"Butuh {lo} sampai {hi} pemain."})
            elif t == "leave":
                if code:
                    await remove(code, uid)
                code = None
                await send(ws, {"t": "left"})
    finally:
        r = ROOMS.get(code) if code else None
        if user and r and user["id"] in r["players"] and r["players"][user["id"]]["ws"] is ws:
            r["players"][user["id"]]["ws"] = None
            await push(code)
            spawn(expire(code, user["id"]))
    return ws


async def on_startup(app):
    try:
        async with ClientSession(timeout=ClientTimeout(total=10)) as s:
            async with s.get(f"https://api.telegram.org/bot{TOKEN}/getMe") as r:
                BOT["username"] = (await r.json())["result"]["username"]
    except Exception:
        pass


async def index(_):
    return web.FileResponse(STATIC / "index.html")


async def health(_):
    return web.Response(text="ok")


app = web.Application()
app.on_startup.append(on_startup)
app.router.add_get("/", index)
app.router.add_get("/healthz", health)
app.router.add_get("/ws", ws_handler)
app.router.add_static("/static/", STATIC, show_index=False)

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=8080)
