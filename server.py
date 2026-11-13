import os, json, hmac, hashlib, random, time, asyncio
from pathlib import Path
from urllib.parse import parse_qsl
from aiohttp import web, WSMsgType, ClientSession, ClientTimeout

TOKEN = os.environ["BOT_TOKEN"]
LIMITS = {"tod": (2, 10), "ludo": (2, 4)}
CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
TURN_SECS = 40  # Ludo: diam selama ini, server main otomatis
START = [1, 14, 27, 40]  # petak mulai tiap warna di jalur 52 petak
SAFE = {1, 14, 27, 40, 9, 22, 35, 48}
SEATS = {2: [0, 2], 3: [0, 1, 2], 4: [0, 1, 2, 3]}
REACTS = {"heart", "laugh", "fire", "star", "wow", "like"}
MAX_AUTH_AGE, GRACE = 86400, 40
ROOMS, TASKS, BOT = {}, set(), {"username": ""}
STATIC = Path(__file__).parent / "static"


def verify(raw):
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


def photo(u):
    p = str(u.get("photo_url") or "")
    return p if p.startswith("https://t.me/i/userpic/") else ""


def spawn(coro):
    t = asyncio.create_task(coro)
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)


async def send(ws, obj):
    if ws is not None and not ws.closed:
        try:
            await ws.send_json(obj)
        except Exception:
            pass


async def cast(code, obj):
    for p in list(ROOMS[code]["players"].values()):
        await send(p["ws"], obj)


def fresh(st, spinner=None):
    st.update(phase="idle", target=None, kind=None, subs={}, cards=[])
    if spinner:
        st["spinner"] = spinner


def pub(r):
    st = r["state"]
    if not st:
        return None
    if st["g"] == "ludo":
        return {k: st[k] for k in ("g", "order", "colors", "tokens", "turn", "phase", "dice", "movable", "winner", "rid", "ev", "tries")}
    return {"g": "tod", "order": st["order"], "spinner": st["spinner"], "round": st["round"], "phase": st["phase"],
            "target": st["target"], "spin": st["spin"], "kind": st["kind"],
            "sent": list(st["subs"]), "cards": st["cards"] if st["phase"] == "do" else []}


def cell(ci, p):
    return (START[ci] + p - 1) % 52


def ludo_new(r):
    order = list(r["players"])
    return {"g": "ludo", "order": order, "colors": dict(zip(order, SEATS[len(order)])),
            "tokens": {u: [0, 0, 0, 0] for u in order}, "turn": order[0], "phase": "roll",
            "dice": None, "movable": [], "winner": None, "rid": 0, "ev": None, "tries": 0, "ver": 0}


def ludo_next(st):
    st["ver"] += 1
    o = st["order"]
    st["turn"] = o[(o.index(st["turn"]) + 1) % len(o)]
    st["phase"], st["movable"], st["tries"] = "roll", [], 0


def ludo_roll(st):
    st["ver"] += 1
    u, d = st["turn"], random.randint(1, 6)
    st["dice"], st["rid"], st["ev"] = d, st["rid"] + 1, None
    st["movable"] = [i for i, p in enumerate(st["tokens"][u]) if (p == 0 and d == 6) or (0 < p < 57 and p + d <= 57)]
    if st["movable"]:
        st["phase"] = "move"
    elif all(p == 0 for p in st["tokens"][u]) and st["tries"] < 2:
        st["tries"] += 1  # semua bidak masih di kandang: boleh lempar sampai 3 kali
        st["ev"] = "retry"
    else:
        st["ev"] = "nomove"
        ludo_next(st)


def ludo_move(st, i):
    u, d = st["turn"], st["dice"]
    st["tries"] = 0
    st["ver"] += 1
    ci, tk, ev = st["colors"][u], st["tokens"][u], None
    tk[i] = 1 if tk[i] == 0 else tk[i] + d
    if tk[i] == 57:
        ev = "finish"
    elif tk[i] <= 51 and cell(ci, tk[i]) not in SAFE:
        for o in st["order"]:
            if o != u:
                for k, q in enumerate(st["tokens"][o]):
                    if 0 < q <= 51 and cell(st["colors"][o], q) == cell(ci, tk[i]):
                        st["tokens"][o][k], ev = 0, "capture"
    if all(p == 57 for p in tk):
        st["phase"], st["winner"], st["movable"], st["ev"] = "over", u, [], "win"
        return
    st["movable"], st["ev"] = [], ev or ("six" if d == 6 else None)
    if d == 6 or ev:
        st["phase"] = "roll"
    else:
        ludo_next(st)


def ludo_act(r, st, uid, host, t, msg):
    if t == "roll" and st["phase"] == "roll" and uid == st["turn"]:
        ludo_roll(st)
    elif t == "move" and st["phase"] == "move" and uid == st["turn"] and msg.get("tk") in st["movable"]:
        ludo_move(st, msg["tk"])
    elif t == "again" and host and st["phase"] == "over":
        r["state"] = ludo_new(r)
    else:
        return False
    return True


async def push0(code):
    r = ROOMS.get(code)
    if r:
        await cast(code, {"t": "room", "code": code, "game": r["game"], "host": r["host"],
                          "started": r["started"], "state": pub(r),
                          "players": [{"id": i, "name": p["name"], "photo": p["photo"],
                                       "on": p["ws"] is not None} for i, p in r["players"].items()]})


async def push(code):
    await push0(code)
    arm(code)


def arm(code, delay=None):
    r = ROOMS.get(code)
    st = r and r["state"]
    if st and st["g"] == "ludo" and st["phase"] in ("roll", "move") and st.get("armed") != st["ver"]:
        st["armed"] = st["ver"]
        spawn(autoplay(code, st["ver"], delay or TURN_SECS))


async def autoplay(code, ver, delay):
    await asyncio.sleep(delay)
    r = ROOMS.get(code)
    st = r and r["state"]
    if not st or st["g"] != "ludo" or st["ver"] != ver or st["phase"] not in ("roll", "move"):
        return
    if st["phase"] == "roll":
        ludo_roll(st)
    else:
        ludo_move(st, random.choice(st["movable"]))
    await push0(code)
    arm(code, 3 if st["phase"] == "move" else None)


def maybe_reveal(r):
    st = r["state"]
    if not st or st["g"] != "tod" or st["phase"] != "write":
        return
    need = [u for u in st["order"] if u != st["target"] and r["players"][u]["ws"] is not None]
    if st["subs"] and all(u in st["subs"] for u in need):
        st["cards"] = random.sample(list(st["subs"].values()), len(st["subs"]))
        st["phase"] = "do"


async def remove(code, uid):
    r = ROOMS.get(code)
    if not r or uid not in r["players"]:
        return
    del r["players"][uid]
    if not r["players"]:
        del ROOMS[code]
        return
    if r["host"] == uid:
        on = [i for i, p in r["players"].items() if p["ws"] is not None]
        r["host"] = on[0] if on else next(iter(r["players"]))
    st = r["state"]
    if st and uid in st["order"]:
        i, was = st["order"].index(uid), st.get("turn") == uid
        st["order"].remove(uid)
        if st["g"] == "ludo":
            st["tokens"].pop(uid, None)
            if len(st["order"]) < 2:
                r["started"], r["state"] = False, None
            elif was:
                st["turn"], st["phase"], st["movable"] = st["order"][i % len(st["order"])], "roll", []
                st["ver"] += 1
        else:
            st["subs"].pop(uid, None)
            if len(st["order"]) < 2:
                r["started"], r["state"] = False, None
            else:
                if st["spinner"] == uid:
                    st["spinner"] = st["order"][0]
                if st["target"] == uid:
                    fresh(st)
                maybe_reveal(r)
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


async def play(code, uid, t, msg):
    r = ROOMS[code]
    st = r["state"]
    host = r["host"] == uid
    if t == "end" and host:
        r["started"], r["state"] = False, None
    elif not st:
        return
    elif st["g"] == "ludo":
        if not ludo_act(r, st, uid, host, t, msg):
            return
    elif t == "spin" and st["phase"] == "idle" and uid == st["spinner"]:
        st["target"] = random.choice(st["order"])
        st["kind"] = random.choice(("truth", "dare"))  # dipilih sistem supaya adil
        st["spin"] += 1
        st["subs"], st["phase"] = {}, "write"
    elif t == "submit" and st["phase"] == "write" and uid in st["order"] and uid != st["target"]:
        text = str(msg.get("text", "")).strip()[:120]
        if text:
            st["subs"][uid] = text
            maybe_reveal(r)
    elif t == "reveal" and host and st["phase"] == "write" and st["subs"]:
        st["cards"] = random.sample(list(st["subs"].values()), len(st["subs"]))
        st["phase"] = "do"
    elif t == "done" and st["phase"] == "do" and uid == st["target"]:
        st["round"] += 1
        fresh(st, uid)
    else:
        return
    await push(code)


async def ws_handler(req):
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=4096)
    await ws.prepare(req)
    user = code = None
    last = 0.0
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
            uid, name, pic = user["id"], who(user), photo(user)
            if t == "create" and msg.get("game") in LIMITS:
                if code:
                    await remove(code, uid)
                code = new_code()
                ROOMS[code] = {"game": msg["game"], "host": uid, "started": False, "state": None,
                               "players": {uid: {"name": name, "photo": pic, "ws": ws}}}
                await push(code)
            elif t == "join":
                c = str(msg.get("code", "")).upper().strip()
                r = ROOMS.get(c)
                if not r:
                    await send(ws, {"t": "error", "c": "notfound", "m": "Kode room tidak ditemukan."})
                elif uid not in r["players"] and r["started"]:
                    await send(ws, {"t": "error", "m": "Game sudah dimulai."})
                elif uid not in r["players"] and len(r["players"]) >= LIMITS[r["game"]][1]:
                    await send(ws, {"t": "error", "m": "Room sudah penuh."})
                else:
                    if code and code != c:
                        await remove(code, uid)
                    code = c
                    r["players"][uid] = {"name": name, "photo": pic, "ws": ws}
                    await push(c)
            elif t == "start" and code in ROOMS:
                r = ROOMS[code]
                lo, hi = LIMITS[r["game"]]
                if r["host"] == uid and not r["started"]:
                    if lo <= len(r["players"]) <= hi:
                        r["started"] = True
                        if r["game"] == "ludo":
                            r["state"] = ludo_new(r)
                        else:
                            r["state"] = {"g": "tod", "order": list(r["players"]), "spinner": uid, "round": 1, "spin": 0}
                            fresh(r["state"])
                        await push(code)
                    else:
                        await send(ws, {"t": "error", "m": f"Butuh {lo} sampai {hi} pemain."})
            elif t in ("spin", "submit", "reveal", "done", "end", "roll", "move", "again") and code in ROOMS:
                await play(code, uid, t, msg)
            elif t in ("chat", "react") and code in ROOMS and time.time() - last > 0.35:
                last = time.time()
                if t == "chat":
                    text = str(msg.get("text", "")).strip()[:200]
                    if text:
                        await cast(code, {"t": "chat", "from": uid, "name": name, "text": text})
                elif msg.get("k") in REACTS:
                    await cast(code, {"t": "react", "from": uid, "k": msg["k"]})
            elif t == "leave":
                if code:
                    await remove(code, uid)
                code = None
                await send(ws, {"t": "left"})
    finally:
        r = ROOMS.get(code) if code else None
        if user and r and user["id"] in r["players"] and r["players"][user["id"]]["ws"] is ws:
            r["players"][user["id"]]["ws"] = None
            maybe_reveal(r)
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


INDEX = (STATIC / "index.html").read_bytes()


async def index(_):
    r = web.Response(body=INDEX, content_type="text/html", charset="utf-8", headers={"Cache-Control": "no-cache"})
    r.enable_compression()
    return r


async def health(_):
    return web.Response(text="ok")


app = web.Application()
app.on_startup.append(on_startup)
app.router.add_get("/", index)
app.router.add_get("/healthz", health)
app.router.add_get("/ws", ws_handler)

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=8080)
