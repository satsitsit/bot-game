import os, json, hmac, hashlib, random, time, asyncio
from pathlib import Path
from urllib.parse import parse_qsl
from aiohttp import web, WSMsgType, ClientSession, ClientTimeout

TOKEN = os.environ["BOT_TOKEN"]
LIMITS = {"tod": (2, 10), "ludo": (2, 4), "mono": (2, 6)}
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
    if st["g"] == "mono":
        return mono_pub(st)
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


# ---------------- Monopoly (papan, harga, dan sewa mengikuti struktur klasik; nama petak dan kartu buatan sendiri) ----------------
def _P(n, g, c, h, r): return {"t": "p", "n": n, "g": g, "c": c, "h": h, "r": r}
def _R(n): return {"t": "r", "n": n, "c": 200}
def _U(n): return {"t": "u", "n": n, "c": 150}
def _X(t, n, c=0): return {"t": t, "n": n, "c": c}
BOARD = [_X("go", "MULAI"), _P("Kupang", "br", 60, 50, [2, 10, 30, 90, 160, 250]), _X("chest", "Dana Warga"),
    _P("Ambon", "br", 60, 50, [4, 20, 60, 180, 320, 450]), _X("tax", "Pajak Penghasilan", 200), _R("Stasiun Gambir"),
    _P("Palu", "lb", 100, 50, [6, 30, 90, 270, 400, 550]), _X("chance", "Kejutan"),
    _P("Manado", "lb", 100, 50, [6, 30, 90, 270, 400, 550]), _P("Jayapura", "lb", 120, 50, [8, 40, 100, 300, 450, 600]),
    _X("jail", "Penjara"), _P("Pontianak", "pk", 140, 100, [10, 50, 150, 450, 625, 750]), _U("Perusahaan Listrik"),
    _P("Banjarmasin", "pk", 140, 100, [10, 50, 150, 450, 625, 750]), _P("Samarinda", "pk", 160, 100, [12, 60, 180, 500, 700, 900]),
    _R("Stasiun Pasar Senen"), _P("Mataram", "or", 180, 100, [14, 70, 200, 550, 750, 950]), _X("chest", "Dana Warga"),
    _P("Pekanbaru", "or", 180, 100, [14, 70, 200, 550, 750, 950]), _P("Padang", "or", 200, 100, [16, 80, 220, 600, 800, 1000]),
    _X("park", "Rest Area"), _P("Palembang", "rd", 220, 150, [18, 90, 250, 700, 875, 1050]), _X("chance", "Kejutan"),
    _P("Makassar", "rd", 220, 150, [18, 90, 250, 700, 875, 1050]), _P("Medan", "rd", 240, 150, [20, 100, 300, 750, 925, 1100]),
    _R("Stasiun Tugu"), _P("Semarang", "yl", 260, 150, [22, 110, 330, 800, 975, 1150]),
    _P("Yogyakarta", "yl", 260, 150, [22, 110, 330, 800, 975, 1150]), _U("Perusahaan Air"),
    _P("Denpasar", "yl", 280, 150, [24, 120, 360, 850, 1025, 1200]), _X("gojail", "Masuk Penjara"),
    _P("Bandung", "gr", 300, 200, [26, 130, 390, 900, 1100, 1275]), _P("Surabaya", "gr", 300, 200, [26, 130, 390, 900, 1100, 1275]),
    _X("chest", "Dana Warga"), _P("Batam", "gr", 320, 200, [28, 150, 450, 1000, 1200, 1400]), _R("Stasiun Gubeng"),
    _X("chance", "Kejutan"), _P("Nusantara", "db", 350, 200, [35, 175, 500, 1100, 1300, 1500]), _X("tax", "Pajak Mewah", 100),
    _P("Jakarta", "db", 400, 200, [50, 200, 600, 1400, 1700, 2000])]
SETS = {}
for _i, _s in enumerate(BOARD):
    if _s["t"] == "p":
        SETS.setdefault(_s["g"], []).append(_i)
RAILS = [i for i, s in enumerate(BOARD) if s["t"] == "r"]
UTILS = [i for i, s in enumerate(BOARD) if s["t"] == "u"]
CHANCE = [("Maju ke MULAI dan terima Rp200", "adv", 0), ("Maju ke Palembang. Lewat MULAI terima Rp200", "adv", 21),
    ("Maju ke Denpasar. Lewat MULAI terima Rp200", "adv", 29), ("Maju ke stasiun terdekat. Sewa dua kali lipat kalau ada pemiliknya", "rail", 0),
    ("Maju ke perusahaan terdekat. Bayar 10 kali lemparan dadu kalau ada pemiliknya", "util", 0), ("Dividen saham cair. Terima Rp50", "cash", 50),
    ("Mundur 3 petak", "back", 3), ("Masuk penjara langsung, tidak lewat MULAI", "jail", 0),
    ("Biaya perbaikan: Rp25 per rumah, Rp100 per hotel", "rep", (25, 100)), ("Denda ngebut. Bayar Rp15", "cash", -15),
    ("Kamu terpilih jadi ketua RT. Bayar Rp50 ke tiap pemain", "each", -50), ("Kartu bebas dari penjara. Simpan sampai dipakai", "free", 0)]
COMMUNITY = [("Salah hitung bank menguntungkanmu. Terima Rp200", "cash", 200), ("Biaya dokter. Bayar Rp50", "cash", -50),
    ("Jual saham. Terima Rp50", "cash", 50), ("Dana liburan cair. Terima Rp100", "cash", 100),
    ("Kartu bebas dari penjara. Simpan sampai dipakai", "free", 0), ("Masuk penjara langsung, tidak lewat MULAI", "jail", 0),
    ("Ulang tahunmu. Terima Rp10 dari tiap pemain", "each", 10), ("Asuransi jiwa cair. Terima Rp100", "cash", 100),
    ("Biaya rumah sakit. Bayar Rp100", "cash", -100), ("Uang sekolah. Bayar Rp50", "cash", -50),
    ("Jasa konsultasi. Terima Rp25", "cash", 25), ("Perbaikan jalan: Rp40 per rumah, Rp115 per hotel", "rep", (40, 115))]
FREEIDX = {"k": 11, "d": 4}


def rp(n): return "Rp" + f"{n:,}".replace(",", ".")
def log(st, text): st["ev"] = (st["ev"] + [text])[-6:]
def own_of(st, i): o = st["own"].get(i); return o["o"] if o else None
def owns_set(st, u, g): return all(own_of(st, j) == u for j in SETS[g])


def mono_new(r):
    order = list(r["players"])
    ks, ds = list(range(len(CHANCE))), list(range(len(COMMUNITY)))
    random.shuffle(ks), random.shuffle(ds)
    return {"g": "mono", "order": order, "nm": {u: r["players"][u]["name"] for u in order},
            "colors": {u: i for i, u in enumerate(order)}, "cash": {u: 1500 for u in order}, "pos": {u: 0 for u in order},
            "jail": {}, "free": {}, "own": {}, "dk": {"k": ks, "d": ds}, "turn": order[0], "phase": "roll",
            "dice": [0, 0], "dbl": 0, "again": False, "rid": 0, "ev": [], "debt": None, "buy": None,
            "winner": None, "dead": [], "ver": 0, "card": None, "cardn": 0,
            "end": time.time() + r.get("mins", 60) * 60 if r.get("mins", 60) else None}


def worth(st, u):
    v = st["cash"][u]
    for i, o in st["own"].items():
        if o["o"] == u:
            v += (BOARD[i]["c"] // 2 if o["m"] else BOARD[i]["c"]) + o["h"] * BOARD[i].get("h", 0)
    return v


def ranking(st):
    return sorted(([u, worth(st, u) if u in st["order"] else 0] for u in st["nm"]), key=lambda x: -x[1])


def mono_pub(st):
    d = {k: st[k] for k in ("g", "order", "nm", "colors", "cash", "pos", "jail", "free", "own", "turn", "phase",
                            "dice", "again", "rid", "ev", "debt", "buy", "winner", "dead", "card")}
    d["board"] = BOARD
    d["left"] = max(0, int(st["end"] - time.time())) if st["end"] else None
    if st["phase"] == "over":
        d["rank"] = ranking(st)
    return d


def rent(st, i, total, x2=False, u10=False):
    s, o = BOARD[i], st["own"][i]
    if s["t"] == "p":
        if o["h"]:
            return s["r"][o["h"]]
        return s["r"][0] * (2 if owns_set(st, o["o"], s["g"]) else 1)
    if s["t"] == "r":
        v = 25 * 2 ** (sum(own_of(st, j) == o["o"] for j in RAILS) - 1)
        return v * 2 if x2 else v
    return total * (10 if u10 or all(own_of(st, j) == o["o"] for j in UTILS) else 4)


def liquid(st, u):
    v = 0
    for i, o in st["own"].items():
        if o["o"] == u:
            v += o["h"] * BOARD[i].get("h", 0) // 2 + (0 if o["m"] else BOARD[i]["c"] // 2)
    return v


def bankrupt(st, u, to):
    log(st, f"{st['nm'][u]} bangkrut")
    for i, o in list(st["own"].items()):
        if o["o"] == u:
            if to is None or to == u:
                del st["own"][i]
            else:
                o["o"], o["h"] = to, 0
    if to is not None and to != u:
        st["cash"][to] += st["cash"][u]
    st["cash"][u] = 0
    i = st["order"].index(u)
    st["order"].remove(u)
    st["dead"].append(u)
    st["debt"], st["buy"] = None, None
    if len(st["order"]) == 1:
        st["phase"], st["winner"] = "over", st["order"][0]
    elif st["turn"] == u:
        st["turn"], st["phase"], st["dbl"], st["again"] = st["order"][i % len(st["order"])], "roll", 0, False


def pay(st, u, amt, to=None):
    if amt <= 0:
        return True
    if st["cash"][u] >= amt:
        st["cash"][u] -= amt
        if to is not None:
            st["cash"][to] += amt
        return True
    if st["cash"][u] + liquid(st, u) < amt:
        bankrupt(st, u, to)
    else:
        st["debt"], st["phase"] = {"to": to, "amt": amt}, "debt"
    return False


def settle(st):
    d, u = st["debt"], st["turn"]
    if d and st["cash"][u] >= d["amt"]:
        st["cash"][u] -= d["amt"]
        if d["to"] is not None:
            st["cash"][d["to"]] += d["amt"]
        st["debt"], st["phase"] = None, "act"
        p = st.pop("pend", None)
        if p:
            move(st, u, p)


def send_jail(st, u):
    st["pos"][u], st["jail"][u], st["again"], st["dbl"] = 10, 0, False, 0
    log(st, f"{st['nm'][u]} masuk penjara")


def go_to(st, u, idx, total, x2=False, u10=False):
    old = st["pos"][u]
    st["pos"][u] = idx
    if idx < old:
        st["cash"][u] += 200
        log(st, f"{st['nm'][u]} melewati MULAI, terima Rp200")
    land(st, u, idx, total, x2, u10)


def move(st, u, total):
    old = st["pos"][u]
    new = (old + total) % 40
    st["pos"][u] = new
    if new < old:
        st["cash"][u] += 200
        log(st, f"{st['nm'][u]} melewati MULAI, terima Rp200")
    land(st, u, new, total)


def draw(st, u, kind, total):
    k = "k" if kind == "chance" else "d"
    deck = CHANCE if k == "k" else COMMUNITY
    i = st["dk"][k].pop(0)
    text, eff, v = deck[i]
    if eff != "free":
        st["dk"][k].append(i)
    log(st, f"{st['nm'][u]} ({'Kejutan' if k == 'k' else 'Dana Warga'}): {text}")
    st["cardn"] += 1
    st["card"] = {"u": u, "k": k, "t": text, "n": st["cardn"]}
    old, others = st["pos"][u], [o for o in st["order"] if o != u]
    if eff == "adv":
        go_to(st, u, v, total)
    elif eff == "rail":
        go_to(st, u, min((j for j in RAILS if j > old), default=RAILS[0]), total, x2=True)
    elif eff == "util":
        go_to(st, u, min((j for j in UTILS if j > old), default=UTILS[0]), total, u10=True)
    elif eff == "back":
        st["pos"][u] = (old - v) % 40
        land(st, u, st["pos"][u], total)
    elif eff == "jail":
        send_jail(st, u)
    elif eff == "free":
        st["free"].setdefault(u, []).append(k)
    elif eff == "cash":
        if v > 0:
            st["cash"][u] += v
        else:
            pay(st, u, -v)
    elif eff == "each":
        if v > 0:
            for o in others:
                m = min(v, st["cash"][o])
                st["cash"][o] -= m
                st["cash"][u] += m
        elif pay(st, u, -v * len(others)):
            for o in others:
                st["cash"][o] += -v
    elif eff == "rep":
        hs = sum(o["h"] for o in st["own"].values() if o["o"] == u and o["h"] < 5)
        ht = sum(1 for o in st["own"].values() if o["o"] == u and o["h"] == 5)
        pay(st, u, v[0] * hs + v[1] * ht)


def land(st, u, idx, total, x2=False, u10=False):
    s, nm = BOARD[idx], st["nm"][u]
    if s["t"] in ("p", "r", "u"):
        o = st["own"].get(idx)
        if o is None:
            if st["cash"][u] >= s["c"]:
                st["phase"], st["buy"] = "buy", idx
            else:
                log(st, f"{nm} tidak cukup uang untuk membeli {s['n']}")
        elif o["o"] != u and not o["m"]:
            amt = rent(st, idx, total, x2, u10)
            log(st, f"{nm} bayar sewa {rp(amt)} ke {st['nm'][o['o']]} di {s['n']}")
            pay(st, u, amt, o["o"])
    elif s["t"] == "tax":
        log(st, f"{nm} bayar {s['n']} {rp(s['c'])}")
        pay(st, u, s["c"])
    elif s["t"] in ("chance", "chest"):
        draw(st, u, s["t"], total)
    elif s["t"] == "gojail":
        send_jail(st, u)


def mono_roll(st):
    u = st["turn"]
    a, b = random.randint(1, 6), random.randint(1, 6)
    dbl, total = a == b, a + b
    st["dice"], st["rid"], st["phase"], st["again"] = [a, b], st["rid"] + 1, "act", False
    if u in st["jail"]:
        if dbl:
            del st["jail"][u]
            st["dbl"] = 0
            log(st, f"{st['nm'][u]} keluar penjara dengan angka kembar")
        else:
            st["jail"][u] += 1
            if st["jail"][u] < 3:
                log(st, f"{st['nm'][u]} gagal keluar penjara")
                return
            del st["jail"][u]
            log(st, f"{st['nm'][u]} bayar Rp50 untuk keluar penjara")
            if not pay(st, u, 50):
                if st["phase"] == "debt":
                    st["pend"] = total
                return
    elif dbl:
        st["dbl"] += 1
        if st["dbl"] >= 3:
            send_jail(st, u)
            return
        st["again"] = True
    else:
        st["dbl"] = 0
    move(st, u, total)


def nxt(st):
    if st.get("end") and time.time() >= st["end"]:
        rk = ranking(st)
        st["phase"], st["winner"], st["again"], st["buy"] = "over", rk[0][0], False, None
        log(st, f"Waktu habis. {st['nm'][rk[0][0]]} paling kaya dengan {rp(rk[0][1])}")
        return
    o = st["order"]
    st["turn"] = o[(o.index(st["turn"]) + 1) % len(o)]
    st["phase"], st["dbl"], st["again"], st["buy"] = "roll", 0, False, None


def manage(st, u, t, i):
    if not 0 <= i < 40 or BOARD[i]["t"] not in ("p", "r", "u"):
        return "Petak tidak valid."
    s, o = BOARD[i], st["own"].get(i)
    if not o or o["o"] != u:
        return "Itu bukan propertimu."
    grp = SETS.get(s.get("g"), [])
    hs = [st["own"].get(j, {"h": 0})["h"] for j in grp]
    if t == "build":
        if s["t"] != "p":
            return "Hanya petak berwarna yang bisa dibangun."
        if not owns_set(st, u, s["g"]):
            return "Kamu belum memiliki semua petak warna ini."
        if any(st["own"][j]["m"] for j in grp):
            return "Ada petak yang digadai di warna ini."
        if o["h"] >= 5:
            return "Sudah hotel."
        if o["h"] > min(hs):
            return "Bangun merata dulu di petak lain."
        if st["cash"][u] < s["h"]:
            return "Uangmu tidak cukup."
        st["cash"][u] -= s["h"]
        o["h"] += 1
        log(st, f"{st['nm'][u]} membangun di {s['n']}")
    elif t == "sell":
        if o["h"] <= 0:
            return "Tidak ada bangunan."
        if o["h"] < max(hs):
            return "Jual merata dulu di petak lain."
        o["h"] -= 1
        st["cash"][u] += s["h"] // 2
    elif t == "mort":
        if o["m"]:
            return "Sudah digadai."
        if any(hs):
            return "Jual semua bangunan di warna ini dulu."
        o["m"] = True
        st["cash"][u] += s["c"] // 2
    elif t == "unmort":
        cost = (s["c"] // 2 * 11 + 9) // 10
        if not o["m"]:
            return "Tidak sedang digadai."
        if st["cash"][u] < cost:
            return "Uangmu tidak cukup."
        o["m"] = False
        st["cash"][u] -= cost
    return None


def mono_act(r, st, uid, host, t, msg):
    if t == "again":
        if host and st["phase"] == "over":
            if len(r["players"]) < 2:
                return "Butuh minimal 2 pemain."
            r["state"] = mono_new(r)
            return None
        return "Belum bisa."
    if st["phase"] == "over":
        return "Game sudah selesai."
    if uid != st["turn"]:
        return "Bukan giliranmu."
    u, ph = uid, st["phase"]
    i = msg.get("i") if isinstance(msg.get("i"), int) else -1
    if t == "roll":
        if ph == "roll" or (ph == "act" and st["again"]):
            mono_roll(st)
            return None
        return "Belum waktunya melempar dadu."
    if t == "buy":
        if ph != "buy":
            return "Tidak ada yang bisa dibeli."
        s = BOARD[st["buy"]]
        if st["cash"][u] < s["c"]:
            return "Uangmu tidak cukup."
        st["cash"][u] -= s["c"]
        st["own"][st["buy"]] = {"o": u, "h": 0, "m": False}
        log(st, f"{st['nm'][u]} membeli {s['n']} seharga {rp(s['c'])}")
        st["buy"], st["phase"] = None, "act"
        return None
    if t == "nobuy":
        if ph != "buy":
            return "Tidak ada yang bisa dilewati."
        st["buy"], st["phase"] = None, "act"
        return None
    if t == "next":
        if ph != "act" or st["again"]:
            return "Belum bisa mengakhiri giliran."
        nxt(st)
        return None
    if t == "jailpay":
        if ph != "roll" or u not in st["jail"]:
            return "Kamu tidak sedang di penjara."
        if st["cash"][u] < 50:
            return "Uangmu tidak cukup."
        st["cash"][u] -= 50
        del st["jail"][u]
        log(st, f"{st['nm'][u]} bayar Rp50 untuk keluar penjara")
        return None
    if t == "jailcard":
        if ph != "roll" or u not in st["jail"] or not st["free"].get(u):
            return "Tidak bisa memakai kartu."
        k = st["free"][u].pop()
        st["dk"][k].append(FREEIDX[k])
        del st["jail"][u]
        log(st, f"{st['nm'][u]} memakai kartu bebas dari penjara")
        return None
    if t == "bankrupt":
        bankrupt(st, u, st["debt"]["to"] if st["debt"] else None)
        return None
    if t in ("build", "sell", "mort", "unmort"):
        if ph not in ("roll", "act", "debt"):
            return "Belum bisa mengelola properti sekarang."
        err = manage(st, u, t, i)
        if err:
            return err
        if ph == "debt":
            settle(st)
        return None
    return "Aksi tidak dikenal."


def mono_auto(st):
    ph, u = st["phase"], st["turn"]
    if ph == "roll" or (ph == "act" and st["again"]):
        mono_roll(st)
    elif ph == "buy":
        st["buy"], st["phase"] = None, "act"
    elif ph == "act":
        nxt(st)
    elif ph == "debt":
        bankrupt(st, u, st["debt"]["to"])


def mono_leave(st, uid, i, was):
    for k in [k for k, o in st["own"].items() if o["o"] == uid]:
        del st["own"][k]
    st["cash"][uid], st["debt"] = 0, None
    st["dead"].append(uid)
    if len(st["order"]) < 2:
        st["phase"], st["winner"] = "over", (st["order"][0] if st["order"] else None)
    elif was:
        st["turn"], st["phase"], st["dbl"], st["again"], st["buy"] = st["order"][i % len(st["order"])], "roll", 0, False, None
    st["ver"] += 1


async def push0(code):
    r = ROOMS.get(code)
    if r:
        await cast(code, {"t": "room", "code": code, "game": r["game"], "host": r["host"],
                          "started": r["started"], "state": pub(r),
                          "board": BOARD if r["game"] == "mono" else None, "mins": r.get("mins", 60) if r["game"] == "mono" else None,
                          "players": [{"id": i, "name": p["name"], "photo": p["photo"],
                                       "on": p["ws"] is not None} for i, p in r["players"].items()]})


async def push(code):
    await push0(code)
    arm(code)


LIVE = {"ludo": ("roll", "move"), "mono": ("roll", "buy", "act", "debt")}


def arm(code, delay=None):
    r = ROOMS.get(code)
    st = r and r["state"]
    if st and st["g"] in LIVE and st["phase"] in LIVE[st["g"]] and st.get("armed") != st["ver"]:
        st["armed"] = st["ver"]
        spawn(autoplay(code, st["ver"], delay or (60 if st["g"] == "mono" else TURN_SECS)))


async def autoplay(code, ver, delay):
    await asyncio.sleep(delay)
    r = ROOMS.get(code)
    st = r and r["state"]
    if not st or st["g"] not in LIVE or st["ver"] != ver or st["phase"] not in LIVE[st["g"]]:
        return
    if st["g"] == "mono":
        mono_auto(st)
        st["ver"] += 1
    elif st["phase"] == "roll":
        ludo_roll(st)
    else:
        ludo_move(st, random.choice(st["movable"]))
    await push0(code)
    arm(code, 3 if st["g"] == "ludo" and st["phase"] == "move" else None)


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
        elif st["g"] == "mono":
            mono_leave(st, uid, i, was)
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
    elif st["g"] == "mono":
        err = mono_act(r, st, uid, host, t, msg)
        if err:
            return err
        r["state"]["ver"] += 1
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
            elif t == "opt" and code in ROOMS:
                r = ROOMS[code]
                if r["host"] == uid and not r["started"] and r["game"] == "mono" and msg.get("mins") in (0, 30, 60, 90):
                    r["mins"] = msg["mins"]
                    await push(code)
            elif t == "start" and code in ROOMS:
                r = ROOMS[code]
                lo, hi = LIMITS[r["game"]]
                if r["host"] == uid and not r["started"]:
                    if lo <= len(r["players"]) <= hi:
                        r["started"] = True
                        if r["game"] == "ludo":
                            r["state"] = ludo_new(r)
                        elif r["game"] == "mono":
                            r["state"] = mono_new(r)
                        else:
                            r["state"] = {"g": "tod", "order": list(r["players"]), "spinner": uid, "round": 1, "spin": 0}
                            fresh(r["state"])
                        await push(code)
                    else:
                        await send(ws, {"t": "error", "m": f"Butuh {lo} sampai {hi} pemain."})
            elif t in ("spin", "submit", "reveal", "done", "end", "roll", "move", "again", "buy", "nobuy", "next", "build", "sell", "mort", "unmort", "jailpay", "jailcard", "bankrupt") and code in ROOMS:
                err = await play(code, uid, t, msg)
                if err:
                    await send(ws, {"t": "error", "m": err})
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
