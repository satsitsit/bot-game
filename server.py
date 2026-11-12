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
PROMPTS = {
    "truth": [
        "Hal paling memalukan yang pernah kamu alami di sekolah apa?",
        "Pernah pura-pura sakit biar nggak masuk sekolah atau kerja? Ceritakan.",
        "Kebohongan terbesar yang pernah kamu bilang ke orang tua apa?",
        "Kebiasaan aneh apa yang kamu kira cuma kamu yang punya?",
        "Lagu apa yang paling malu kamu akui kalau sering kamu dengerin?",
        "Hewan apa yang ingin kamu jadi selama sehari, dan kenapa?",
        "Apa yang paling kamu takutkan tapi jarang kamu bilang?",
        "Barang apa yang paling sering kamu cari tapi nggak ketemu?",
    ],
    "dare": [
        "Kirim sticker yang paling sering kamu pakai ke grup, sekarang.",
        "Tirukan suara hewan favoritmu sampai yang lain nebak.",
        "Puji salah satu orang di grup dengan kalimat paling lebay.",
        "Ceritakan lelucon paling garing yang kamu tahu.",
        "Lakukan gerakan dance 10 detik tanpa musik.",
        "Ucapkan nama lengkapmu pelan, lalu cepat, lalu seperti pembawa berita.",
        "Panggil salah satu orang di grup dengan nama panggilan lucu pilihanmu.",
        "Bicara dengan logat daerah lain sampai giliranmu berikutnya.",
    ],
}
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


def public_state(r):
    st = r["state"]
    if not st:
        return None
    return {"order": st["order"], "turn": st["order"][st["turn"]], "round": st["round"],
            "phase": st["phase"], "kind": st["kind"], "prompt": st["prompt"]}


async def push(code):
    r = ROOMS.get(code)
    if not r:
        return
    v = {"t": "room", "code": code, "game": r["game"], "host": r["host"], "started": r["started"],
         "state": public_state(r),
         "players": [{"id": i, "name": p["name"], "on": p["ws"] is not None}
                     for i, p in r["players"].items()]}
    for p in list(r["players"].values()):
        await send(p["ws"], v)


def end_game(r):
    r["started"], r["state"] = False, None


def advance(st):
    st["turn"] = (st["turn"] + 1) % len(st["order"])
    if st["turn"] == 0:
        st["round"] += 1
    st["phase"], st["kind"], st["prompt"] = "choose", None, None


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
    st = r["state"]
    if st and uid in st["order"]:
        i = st["order"].index(uid)
        st["order"].pop(i)
        if len(st["order"]) < 2:
            end_game(r)
        elif i < st["turn"]:
            st["turn"] -= 1
        elif i == st["turn"]:
            st["turn"] = i % len(st["order"])
            st["phase"], st["kind"], st["prompt"] = "choose", None, None
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


async def game_action(code, uid, t, msg):
    r = ROOMS[code]
    st = r["state"]
    if t == "end":
        if r["host"] == uid and r["started"]:
            end_game(r)
            await push(code)
        return
    if not st:
        return
    if t == "skip":
        if r["host"] == uid:
            advance(st)
            await push(code)
        return
    if uid != st["order"][st["turn"]]:
        return
    if t == "pick" and st["phase"] == "choose" and msg.get("kind") in ("truth", "dare"):
        st["kind"] = msg["kind"]
        st["prompt"] = random.choice(PROMPTS[msg["kind"]])
        st["phase"] = "answer"
        await push(code)
    elif t == "done" and st["phase"] == "answer":
        advance(st)
        await push(code)


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
            game = msg.get("game")
            if t == "create" and isinstance(game, str) and game in LIMITS:
                if code:
                    await remove(code, uid)
                code = new_code()
                ROOMS[code] = {"game": game, "host": uid, "started": False, "state": None,
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
                        if r["game"] == "tod":
                            r["state"] = {"order": list(r["players"]), "turn": 0, "round": 1,
                                          "phase": "choose", "kind": None, "prompt": None}
                        await push(code)
                    else:
                        await send(ws, {"t": "error", "c": "count", "m": f"Butuh {lo} sampai {hi} pemain."})
            elif t in ("pick", "done", "skip", "end") and code in ROOMS:
                await game_action(code, uid, t, msg)
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


INDEX = (STATIC / "index.html").read_bytes()  # dibaca sekali saat start, lebih cepat


async def index(_):
    r = web.Response(body=INDEX, content_type="text/html", charset="utf-8",
                     headers={"Cache-Control": "no-cache"})
    r.enable_compression()
    return r


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
