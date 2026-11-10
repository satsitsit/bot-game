import os, json, hmac, hashlib, random, string, asyncio
from pathlib import Path
from collections import Counter
from urllib.parse import parse_qsl
from aiohttp import web, WSMsgType

TOKEN = os.environ["BOT_TOKEN"]
LIMITS = {"ludo": (2, 4), "werewolf": (5, 10), "tod": (2, 10),
          "monyet": (3, 3), "gunung": (2, 8), "distrik": (2, 6)}
ROOMS = {}


def verify(raw):
    try:
        d = dict(parse_qsl(raw, strict_parsing=True))
        got = d.pop("hash")
        s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        ok = hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), got)
        return json.loads(d["user"]) if ok else None
    except Exception:
        return None


def pname(r, uid):
    p = r["players"].get(uid)
    return p["name"] if p else "?"


def bump_win(r, uids):
    if not uids: return
    r.setdefault("wins", {})
    for u in uids:
        r["wins"][u] = r["wins"].get(u, 0) + 1


# ================= DISTRIK =================
D_ZONES = [
    {"n": "Gudang",  "gen": True,  "pallet": True,  "hook": False, "exit": False},
    {"n": "Sekolah", "gen": True,  "pallet": False, "hook": True,  "exit": False},
    {"n": "Toko",    "gen": True,  "pallet": True,  "hook": False, "exit": False},
    {"n": "Rumah",   "gen": True,  "pallet": False, "hook": True,  "exit": False},
    {"n": "Hutan",   "gen": True,  "pallet": True,  "hook": False, "exit": False},
    {"n": "Tambang", "gen": True,  "pallet": False, "hook": True,  "exit": False},
    {"n": "Gereja",  "gen": False, "pallet": True,  "hook": False, "exit": True},
    {"n": "Pos Jaga","gen": False, "pallet": False, "hook": False, "exit": True},
]
D_ADJ = {i: [(i - 1) % 8, (i + 1) % 8] for i in range(8)}
D_GENS_NEEDED = 5
D_ATTACK_TIMEOUT = 28
D_HOOK_STAGES = 2
D_HOOK_TOTAL = 3
KILLER_TYPES = ["slasher", "stalker", "hidden"]
D_ITEMS = {0: ["toolbox"], 2: ["medkit"], 3: ["flashlight"], 4: ["medkit"],
           5: ["toolbox"], 6: ["flashlight"], 7: ["medkit"]}


async def dsend(r, uid, obj):
    p = r["players"].get(uid)
    if p and not p["ws"].closed:
        try: await p["ws"].send_json(obj)
        except Exception: pass


async def dbroadcast(r, obj):
    for uid in list(r["players"]):
        await dsend(r, uid, obj)


async def dstate(r):
    for uid in list(r["players"]):
        await dsend(r, uid, dview(r, uid))


def dview(r, uid):
    s = r["state"]
    is_k = uid == s["killer"]
    kz = s["killer_zone"]
    myz = s["survivors"].get(uid, {}).get("zone")
    dead = s["survivors"].get(uid, {}).get("state") in ("dead",)
    spec = dead or uid == s["killer"]
    vis = {}
    for pid, p in s["survivors"].items():
        if p["state"] in ("dead", "escaped"):
            vis[pid] = dict(p); continue
        same = p["zone"] == kz
        adj = p["zone"] in D_ADJ[kz]
        revealed = p.get("revealed", False)
        if is_k:
            if same or (adj and s["killer_type"] == "stalker") or revealed:
                vis[pid] = dict(p)
            else:
                vis[pid] = {"zone": None, "state": p["state"], "hp": p["hp"],
                            "hook_count": p["hook_count"], "hidden": True,
                            "revealed": False, "items": []}
        else:
            vis[pid] = dict(p)
    heartbeat = 0
    if not spec and myz is not None:
        d = abs(kz - myz) % 8
        heartbeat = 3 if d == 0 else (2 if d in (1, 7) else (1 if d in (2, 6) else 0))
    return {
        "t": "state", "game": "distrik", "you": uid, "is_killer": is_k,
        "is_spectator": dead, "killer_type": s["killer_type"],
        "killer": {"zone": kz if (spec or heartbeat) else None,
                   "carry": s["killer"]["carry"], "stun": s["killer"]["stun"],
                   "ability_cd": s["killer"]["ability_cd"]},
        "survivors": vis, "order": s["order"],
        "names": {pid: pname(r, pid) for pid in s["order"]},
        "gens": s["gens"], "gens_done": s["gens_done"], "need": D_GENS_NEEDED,
        "pallets": s["pallets"], "gate_open": s["gate_open"],
        "round": s["round"], "winner": s["winner"], "log": s["log"][-12:],
        "heartbeat": heartbeat, "turn": s.get("turn_uid"),
        "phase": s.get("phase", "idle"),
        "zones": [{"n": z["n"], "gen": z["gen"], "hook": z["hook"],
                   "exit": z["exit"], "items": s["items"].get(i, [])}
                  for i, z in enumerate(D_ZONES)],
        "wins": r.get("wins", {}),
    }


def distrik_start(r):
    pids = list(r["players"].keys()); random.shuffle(pids)
    killer = pids[0]; ktype = random.choice(KILLER_TYPES)
    kz = random.choice(range(8))
    survivors = {}
    for uid in pids[1:]:
        z = random.choice([x for x in range(8) if x != kz])
        survivors[uid] = {"zone": z, "hp": 2, "state": "healthy", "hook_count": 0,
                          "hook_stage": 0, "hidden": False, "revealed": False,
                          "bleed": 0, "items": []}
    r["state"] = {
        "game": "distrik", "killer": killer, "killer_type": ktype,
        "killer_zone": kz, "survivors": survivors, "order": pids,
        "gens": {i: 0 for i in range(6)}, "gens_done": 0,
        "pallets": {i: True for i in range(8) if D_ZONES[i]["pallet"]},
        "gate_open": False, "winner": None, "round": 1, "scored": False,
        "log": [f"Distrik dimulai. Killer: {ktype.upper()}"],
        "killer": {"zone": kz, "stun": 0, "carry": None, "ability_cd": 0},
        "items": {i: list(v) for i, v in D_ITEMS.items()},
        "queues": {uid: asyncio.Queue() for uid in pids},
        "turn_uid": None, "phase": "idle",
    }
    r["task"] = asyncio.create_task(distrik_loop(r))


async def skillcheck(r, uid, kind, dur=2.4, tol=16):
    sid = random.randint(10000, 99999); target = random.randint(18, 82)
    await dsend(r, uid, {"t": "skillcheck", "id": sid, "kind": kind,
                         "target": target, "tol": tol, "dur": int(dur * 1000)})
    q = r["state"]["queues"].get(uid)
    if not q: return "fail"
    try: msg = await asyncio.wait_for(q.get(), timeout=dur + 0.8)
    except asyncio.TimeoutError: return "fail"
    if msg.get("t") == "skill" and msg.get("id") == sid:
        try: pos = float(msg.get("pos"))
        except Exception: return "fail"
        diff = abs(pos - target)
        if diff <= tol * 0.35: return "perfect"
        if diff <= tol: return "good"
    return "fail"


async def dget_action(r, uid, timeout):
    q = r["state"]["queues"].get(uid)
    if not q: return {"t": "timeout"}
    try: return await asyncio.wait_for(q.get(), timeout=timeout)
    except asyncio.TimeoutError: return {"t": "timeout"}


def _alive_survivors(r):
    return [uid for uid, p in r["state"]["survivors"].items()
            if p["state"] not in ("dead", "escaped")]


def _check_winner(r):
    s = r["state"]
    alive = _alive_survivors(r)
    if any(p["state"] == "escaped" for p in s["survivors"].values()):
        s["winner"] = "survivor"
    elif not alive:
        s["winner"] = "killer"
    if s["winner"] and not s["scored"]:
        s["scored"] = True
        if s["winner"] == "killer": bump_win(r, [s["killer"]])
        else: bump_win(r, [p for p in s["order"] if p != s["killer"]])


async def distrik_loop(r):
    s = r["state"]
    try:
        while not s["winner"]:
            s["phase"] = "killer"; s["turn_uid"] = s["killer"]
            await dbroadcast(r, {"t": "turn", "role": "killer",
                                 "uid": s["killer"], "timer": D_ATTACK_TIMEOUT})
            await dstate(r)
            act = await dget_action(r, s["killer"], D_ATTACK_TIMEOUT)
            if act.get("t") == "timeout":
                targets = [p["zone"] for p in s["survivors"].values()
                           if p["state"] not in ("dead", "escaped") and p["zone"] is not None]
                if targets:
                    tz = min(targets, key=lambda z: min(abs(z - s["killer_zone"]),
                                                        8 - abs(z - s["killer_zone"])))
                    adj = D_ADJ[s["killer_zone"]]
                    step = min(adj, key=lambda z: min(abs(z - tz), 8 - abs(z - tz)))
                    act = {"t": "move", "zone": step}
            await dapply_killer(r, act)
            _check_winner(r)
            if s["winner"]: break
            await dstate(r)
            for uid in s["order"]:
                if uid == s["killer"]: continue
                p = s["survivors"][uid]
                if p["state"] in ("dead", "escaped"): continue
                if p["state"] == "hooked":
                    res = await skillcheck(r, uid, "struggle", 1.6, 18)
                    if res != "fail": p["hook_stage"] = max(0, p["hook_stage"] - 1)
                    await dstate(r); continue
                if p["state"] == "downed": continue
                s["phase"] = "survivor"; s["turn_uid"] = uid
                await dbroadcast(r, {"t": "turn", "role": "survivor",
                                     "uid": uid, "timer": D_ATTACK_TIMEOUT})
                await dstate(r)
                act = await dget_action(r, uid, D_ATTACK_TIMEOUT)
                await dapply_survivor(r, uid, act)
                _check_winner(r)
                if s["winner"]: break
                await dstate(r)
            if s["winner"]: break
            await dround_end(r)
            await dstate(r)
        await dbroadcast(r, {"t": "end", "winner": s["winner"]})
    except asyncio.CancelledError: pass
    except Exception as e:
        s["log"].append(f"Game error: {e}")
        await dstate(r)


async def dapply_killer(r, act):
    s = r["state"]; k = s["killer"]
    if k["stun"] > 0:
        k["stun"] -= 1
        s["log"].append("Killer ter-stun."); return
    t = act.get("t")
    if t == "move":
        z = act.get("zone")
        if not isinstance(z, int) or z not in D_ADJ[k["zone"]]: return
        k["zone"] = z; s["killer_zone"] = z
        if k["carry"]: s["survivors"][k["carry"]]["zone"] = z
        s["log"].append(f"Killer ke {D_ZONES[z]['n']}.")
    elif t == "attack":
        tgt = act.get("target"); p = s["survivors"].get(tgt)
        if not p or p["zone"] != k["zone"] or p["state"] in ("dead", "escaped", "hooked"): return
        if p["hidden"] and not p["revealed"]: return
        if "medkit" in p["items"]:
            p["items"].remove("medkit")
            s["log"].append(f"{pname(r, tgt)} pakai medkit.")
            p["state"] = "healthy"; p["hp"] = 2; p["revealed"] = False; return
        p["hp"] -= 1; p["hidden"] = False
        if p["hp"] <= 0:
            p["state"] = "downed"; p["bleed"] = 2
            s["log"].append(f"{pname(r, tgt)} down!")
        else:
            p["state"] = "injured"; s["log"].append(f"{pname(r, tgt)} terluka.")
    elif t == "pickup":
        tgt = act.get("target"); p = s["survivors"].get(tgt)
        if p and p["state"] == "downed" and p["zone"] == k["zone"] and not k["carry"]:
            k["carry"] = tgt; s["log"].append(f"Angkat {pname(r, tgt)}.")
    elif t == "hook":
        if k["carry"] and D_ZONES[k["zone"]]["hook"]:
            tgt = k["carry"]; p = s["survivors"][tgt]
            p["hook_count"] += 1
            if p["hook_count"] >= D_HOOK_TOTAL:
                p["state"] = "dead"; k["carry"] = None
                s["log"].append(f"{pname(r, tgt)} dikorbankan.")
            else:
                p["state"] = "hooked"; p["hook_stage"] = 0
                p["zone"] = k["zone"]; k["carry"] = None
                s["log"].append(f"{pname(r, tgt)} digantung ({p['hook_count']}/3).")
    elif t == "search":
        found = False
        for pid, p in s["survivors"].items():
            if p["zone"] == k["zone"] and p["hidden"]:
                p["revealed"] = True; found = True
        s["log"].append("Cari." + (" Ketemu!" if found else " Kosong."))
    elif t == "ability":
        if k["ability_cd"] > 0: return
        k["ability_cd"] = 3
        if s["killer_type"] == "slasher":
            k["zone"] = (k["zone"] + 1) % 8; s["killer_zone"] = k["zone"]
        elif s["killer_type"] == "stalker":
            for p in s["survivors"].values(): p["revealed"] = True


async def dapply_survivor(r, uid, act):
    s = r["state"]; p = s["survivors"][uid]; k = s["killer"]
    name = pname(r, uid); t = act.get("t")
    if t == "move":
        z = act.get("zone")
        if not isinstance(z, int) or z not in D_ADJ[p["zone"]]: return
        p["zone"] = z; p["hidden"] = False
        if s["items"].get(z):
            item = s["items"][z].pop(); p["items"].append(item)
        s["log"].append(f"{name} ke {D_ZONES[z]['n']}.")
    elif t == "repair":
        if p["zone"] > 5 or s["gens"][p["zone"]] >= 100: return
        g = p["zone"]; res = await skillcheck(r, uid, "repair")
        add = {"perfect": 25, "good": 15, "fail": 5}[res]
        if "toolbox" in p["items"]: add += 8; p["items"].remove("toolbox")
        s["gens"][g] = min(100, s["gens"][g] + add)
        if res == "fail": p["revealed"] = True; s["log"].append(f"{name} gagal check.")
        elif s["gens"][g] >= 100:
            s["gens_done"] += 1
            s["log"].append(f"Generator selesai ({s['gens_done']}/{D_GENS_NEEDED}).")
        else: s["log"].append(f"{name} repair +{add}%.")
    elif t == "heal":
        tgt = act.get("target")
        if tgt == uid:
            if p["state"] == "injured":
                res = await skillcheck(r, uid, "heal")
                if res != "fail":
                    p["hp"] = 2; p["state"] = "healthy"
                    s["log"].append(f"{name} sembuh.")
            return
        q = s["survivors"].get(tgt)
        if not q or q["zone"] != p["zone"] or q["state"] not in ("injured", "downed"): return
        res = await skillcheck(r, uid, "heal")
        if res != "fail":
            if q["state"] == "downed": q["state"] = "injured"; q["hp"] = 1; q["bleed"] = 0
            else: q["state"] = "healthy"; q["hp"] = 2
            s["log"].append(f"{name} heal {pname(r, tgt)}.")
    elif t == "rescue":
        tgt = act.get("target"); q = s["survivors"].get(tgt)
        if q and q["state"] == "hooked" and q["zone"] == p["zone"]:
            q["state"] = "injured"; q["hp"] = 1; q["hook_stage"] = 0
            s["log"].append(f"{name} selamatkan {pname(r, tgt)}.")
    elif t == "hide":
        p["hidden"] = True; p["revealed"] = False
    elif t == "pallet":
        z = p["zone"]
        if s["pallets"].get(z) and k["zone"] == z:
            s["pallets"][z] = False; k["stun"] = 1
            s["log"].append(f"Pallet — killer stun!")
    elif t == "open_gate":
        if s["gens_done"] >= D_GENS_NEEDED and p["zone"] in (6, 7):
            s["gate_open"] = True; s["log"].append(f"{name} buka gate!")
    elif t == "escape":
        if s["gate_open"] and p["zone"] in (6, 7):
            p["state"] = "escaped"; s["log"].append(f"{name} KABUR!")
    elif t == "flashlight":
        if "flashlight" in p["items"]:
            p["items"].remove("flashlight")
            s["flashlight_until"] = s["round"] + 1


async def dround_end(r):
    s = r["state"]
    for uid, p in s["survivors"].items():
        if p["state"] == "hooked":
            p["hook_stage"] += 1
            if p["hook_stage"] >= D_HOOK_STAGES:
                p["state"] = "dead"
        elif p["state"] == "downed":
            p["bleed"] -= 1
            if p["bleed"] <= 0: p["state"] = "dead"
    for p in s["survivors"].values(): p["revealed"] = False
    if s["killer"]["ability_cd"] > 0: s["killer"]["ability_cd"] -= 1
    s["round"] += 1
    _check_winner(r)


# ================= LUDO =================
def ludo_start(r):
    pids = list(r["players"].keys())
    r["state"] = {"game": "ludo", "order": pids, "turn": 0,
                  "tokens": {p: [-1, -1, -1, -1] for p in pids},
                  "dice": None, "rolled": False, "winner": None, "scored": False,
                  "log": []}


def ludo_offset(r, uid): return r["state"]["order"].index(uid) * 13
def ludo_bpos(r, uid, prog):
    return (ludo_offset(r, uid) + prog) % 52 if 0 <= prog <= 51 else None


def ludo_moves(tokens, d):
    out = []
    for i, p in enumerate(tokens):
        if p == 57: continue
        if p == -1 and d == 6: out.append(i)
        if p != -1 and p + d <= 57: out.append(i)
    return out


def ludo_view(r, uid):
    s = r["state"]; my = s["order"][s["turn"]] == uid
    mv = ludo_moves(s["tokens"][uid], s["dice"]) if my and s["rolled"] and s["dice"] and not s["winner"] else []
    return {"t": "state", "game": "ludo", "order": s["order"], "turn": s["turn"],
            "names": {p: pname(r, p) for p in s["order"]},
            "tokens": s["tokens"], "dice": s["dice"], "rolled": s["rolled"],
            "winner": s["winner"], "log": s["log"][-8:], "you": uid, "moves": mv,
            "wins": r.get("wins", {}),
            "canRoll": my and not s["rolled"] and not s["winner"]}


def ludo_handle(r, uid, msg):
    s = r["state"]; t = msg.get("t")
    if s["winner"]: return
    if t == "roll":
        if s["order"][s["turn"]] != uid: return {"error": "Bukan giliranmu."}
        if s["rolled"]: return {"error": "Sudah lempar."}
        d = random.randint(1, 6); s["dice"] = d; s["rolled"] = True
        s["log"].append(f"{pname(r, uid)} → {d}")
        if not ludo_moves(s["tokens"][uid], d):
            s["rolled"] = False; s["dice"] = None
            s["turn"] = (s["turn"] + 1) % len(s["order"])
    elif t == "move":
        if s["order"][s["turn"]] != uid or not s["rolled"]: return {"error": "Tidak bisa."}
        i = msg.get("tidx")
        if not isinstance(i, int) or i not in ludo_moves(s["tokens"][uid], s["dice"]):
            return {"error": "Token tidak valid."}
        tk = s["tokens"][uid]; p = tk[i]; d = s["dice"]
        tk[i] = 0 if p == -1 else p + d
        if 0 <= tk[i] <= 51:
            bp = ludo_bpos(r, uid, tk[i])
            for op in s["order"]:
                if op == uid: continue
                for j, o in enumerate(s["tokens"][op]):
                    if 0 <= o <= 51 and ludo_bpos(r, op, o) == bp:
                        s["tokens"][op][j] = -1
        if all(x == 57 for x in tk):
            s["winner"] = uid; s["log"].append(f"{pname(r, uid)} menang!")
            if not s.get("scored"): s["scored"] = True; bump_win(r, [uid])
            return
        s["rolled"] = False; s["dice"] = None
        if d != 6: s["turn"] = (s["turn"] + 1) % len(s["order"])


# ================= TRUTH OR DARE =================
TOD_T = ["Sebutkan 3 kelebihan orang di kananmu.", "Ceritakan momen paling memalukan minggu ini.",
         "Siapa orang terakhir yang kamu chat?", "Kebiasaan kecil yang bikin kesal?",
         "Pernah bohong ke sahabat?", "Hal yang kamu sembunyikan dari orang tua?",
         "Nama mantan (kalau ada).", "Pesan terakhir yang kamu hapus?",
         "Ketakutan terbesar?", "Tukar hidup dengan siapa di sini?",
         "Hal paling konyol minggu ini?", "Siapa yang paling kamu kagumi di sini?"]
TOD_D = ["Kirim emoji ke 3 kontak.", "Tirukan hewan 10 detik.",
         "Nyanyikan chorus lagu favorit.", "Screenshot home screen HP.",
         "Telepon kontak acak.", "Push-up 10 kali.",
         "Lelucon paling garing.", "Chat 'aku suka kamu' ke orang pertama.",
         "Tunjukkan 3 foto terakhir.", "Bicara aksen aneh 2 menit.",
         "Tulis nama pakai tangan non-dominan.", "Tepuk tangan 20 detik."]


def tod_start(r):
    r["state"] = {"game": "tod", "order": list(r["players"].keys()), "turn": 0,
                  "phase": "spin", "chosen": None, "mode": None, "prompt": None,
                  "history": []}


def tod_view(r, uid):
    s = r["state"]
    return {"t": "state", "game": "tod", "order": s["order"], "turn": s["turn"],
            "names": {p: pname(r, p) for p in s["order"]},
            "phase": s["phase"], "chosen": s["chosen"], "mode": s["mode"],
            "prompt": s["prompt"], "you": uid, "history": s["history"][-5:],
            "wins": r.get("wins", {}),
            "canSpin": s["phase"] == "spin" and s["order"][s["turn"]] == uid,
            "canChoose": s["phase"] == "choose" and s["chosen"] == uid,
            "canNext": s["phase"] == "prompt" and (s["chosen"] == uid or s["order"][s["turn"]] == uid)}


def tod_handle(r, uid, msg):
    s = r["state"]; t = msg.get("t")
    if t == "spin":
        if s["phase"] != "spin" or s["order"][s["turn"]] != uid: return {"error": "Bukan giliranmu."}
        s["chosen"] = random.choice(s["order"]); s["phase"] = "choose"
    elif t == "choose":
        if s["phase"] != "choose" or s["chosen"] != uid: return {"error": "Bukan giliranmu."}
        m = msg.get("mode")
        if m not in ("truth", "dare"): return {"error": "Pilih truth/dare."}
        s["mode"] = m; s["prompt"] = random.choice(TOD_T if m == "truth" else TOD_D)
        s["phase"] = "prompt"
        s["history"].append({"who": pname(r, s["chosen"]), "mode": m})
    elif t == "next":
        if s["phase"] != "prompt": return
        s["phase"] = "spin"; s["turn"] = (s["turn"] + 1) % len(s["order"])
        s["chosen"] = s["mode"] = s["prompt"] = None


# ================= PANJAT GUNUNG =================
G_EVENTS = [(3, "Mata air panas +3", 3), (7, "Longsor -2", -2),
            (11, "Angin baik +4", 4), (15, "Terpeleset -3", -3),
            (19, "Jalan pintas +2", 2), (23, "Istirahat", 0), (27, "Tali kuat +1", 1)]
G_GOAL = 30


def gunung_start(r):
    pids = list(r["players"].keys())
    r["state"] = {"game": "gunung", "order": pids, "turn": 0,
                  "pos": {p: 0 for p in pids}, "goal": G_GOAL,
                  "dice": None, "rolled": False, "winner": None, "scored": False,
                  "log": [], "skip": []}


def gunung_view(r, uid):
    s = r["state"]
    return {"t": "state", "game": "gunung",
            "order": [{"id": p, "name": pname(r, p), "pos": s["pos"][p]} for p in s["order"]],
            "turn": s["turn"], "goal": s["goal"], "dice": s["dice"],
            "rolled": s["rolled"], "winner": s["winner"], "log": s["log"][-8:],
            "you": uid, "skip": list(s["skip"]), "wins": r.get("wins", {}),
            "canRoll": s["order"][s["turn"]] == uid and not s["rolled"] and not s["winner"]}


def gunung_handle(r, uid, msg):
    s = r["state"]
    if s["winner"] or msg.get("t") != "roll": return
    if s["order"][s["turn"]] != uid or s["rolled"]: return {"error": "Tidak bisa."}
    d = random.randint(1, 6); s["dice"] = d
    cur = s["pos"][uid]; new = cur + d; ev = None
    for pos, emsg, delta in G_EVENTS:
        if cur < pos <= new:
            new = max(0, min(G_GOAL, new + delta)); ev = emsg
            if delta == 0: s["skip"].append(uid)
            break
    s["pos"][uid] = max(0, min(G_GOAL, new))
    s["log"].append(f"{pname(r, uid)} → {s['pos'][uid]}")
    if ev: s["log"].append(ev)
    if s["pos"][uid] >= G_GOAL:
        s["winner"] = uid; s["log"].append(f"{pname(r, uid)} sampai puncak!")
        if not s.get("scored"): s["scored"] = True; bump_win(r, [uid])
        return
    s["rolled"] = False; s["dice"] = None
    s["turn"] = (s["turn"] + 1) % len(s["order"])
    guard = 0
    while s["order"][s["turn"]] in s["skip"] and guard < 10:
        s["skip"].remove(s["order"][s["turn"]])
        s["turn"] = (s["turn"] + 1) % len(s["order"]); guard += 1


# ================= TIGA MONYET =================
M_WORDS = [("MATAHARI", "Bintang siang"), ("GUNUNG", "Tinggi, didaki"),
           ("SEPEDA", "Kendaraan roda dua"), ("KUCING", "Hewan berkumis"),
           ("BUKU", "Jendela dunia"), ("PANTAI", "Pasir dan ombak"),
           ("BULAN", "Bintang malam"), ("APEL", "Buah, dokter jauh"),
           ("KAMERA", "Alat potret"), ("JAKET", "Pakaian penghangat")]


def monyet_start(r):
    r["state"] = {"game": "monyet", "order": list(r["players"].keys()), "round": 0,
                  "total_rounds": 5, "word": None, "hint": None,
                  "fragments": {}, "submitted": {}, "result": None, "log": [],
                  "scored": False}
    _monyet_next(r)


def _monyet_next(r):
    s = r["state"]
    if s["round"] >= s["total_rounds"]: return
    s["round"] += 1
    w, hint = random.choice(M_WORDS)
    s["word"] = w; s["hint"] = hint
    n = len(w); base = n // 3; ex = n % 3
    parts, idx = [], 0
    for i in range(3):
        sz = base + (1 if i < ex else 0)
        parts.append(w[idx:idx + sz]); idx += sz
    s["fragments"] = {p: parts[i] for i, p in enumerate(s["order"][:3])}
    s["submitted"] = {p: None for p in s["order"]}


def monyet_view(r, uid):
    s = r["state"]
    return {"t": "state", "game": "monyet", "round": s["round"],
            "total_rounds": s["total_rounds"], "hint": s["hint"],
            "fragment": s["fragments"].get(uid),
            "submitted": {p: s["submitted"].get(p) is not None for p in s["order"]},
            "order": s["order"], "names": {p: pname(r, p) for p in s["order"]},
            "result": s["result"], "log": s["log"][-8:],
            "you": uid, "your_sub": s["submitted"].get(uid),
            "wins": r.get("wins", {})}


def monyet_handle(r, uid, msg):
    s = r["state"]
    if s["result"] or msg.get("t") != "submit": return
    if s["submitted"].get(uid) is not None: return {"error": "Sudah submit."}
    ans = str(msg.get("answer", "")).upper().strip()
    if not ans: return {"error": "Isi jawaban."}
    s["submitted"][uid] = ans
    if all(v is not None for v in s["submitted"].values()):
        ok = all(v == s["word"] for v in s["submitted"].values())
        if ok:
            s["log"].append(f"R{s['round']}: BENAR! {s['word']}")
            if s["round"] >= s["total_rounds"]:
                s["result"] = "win"
                if not s["scored"]: s["scored"] = True; bump_win(r, list(s["order"]))
            else: _monyet_next(r)
        else:
            s["result"] = "lose"; s["log"].append(f"R{s['round']}: GAGAL. {s['word']}")


# ================= WEREWOLF =================
def werewolf_start(r):
    pids = list(r["players"].keys()); random.shuffle(pids)
    n = len(pids); nw = 2 if n >= 7 else 1
    roles = {}; i = 0
    for _ in range(nw): roles[pids[i]] = "werewolf"; i += 1
    if n >= 5: roles[pids[i]] = "seer"; i += 1
    if n >= 6: roles[pids[i]] = "doctor"; i += 1
    while i < n: roles[pids[i]] = "villager"; i += 1
    r["state"] = {"game": "werewolf", "order": pids, "roles": roles,
                  "alive": set(pids), "phase": "night", "round": 0,
                  "night": {}, "day_votes": {}, "winner": None, "log": [],
                  "seer_log": {}, "doc_last": None, "scored": False}
    _ww_night(r)


def _ww_night(r):
    s = r["state"]; s["round"] += 1; s["phase"] = "night"
    s["night"] = {"wolf": {}, "seer_result": {}, "doc_acted": False, "doc_target": None}
    s["day_votes"] = {}
    s["log"].append(f"Malam {s['round']}")


def _ww_check_night_done(r):
    s = r["state"]
    wolves = [p for p in s["alive"] if s["roles"][p] == "werewolf"]
    seer = next((p for p in s["alive"] if s["roles"][p] == "seer"), None)
    doc = next((p for p in s["alive"] if s["roles"][p] == "doctor"), None)
    n = s["night"]
    if not all(w in n["wolf"] for w in wolves): return False
    if seer and seer not in n["seer_result"]: return False
    if doc and not n["doc_acted"]: return False
    return True


def _ww_resolve_night(r):
    s = r["state"]; n = s["night"]
    victim = Counter(n["wolf"].values()).most_common(1)[0][0] if n["wolf"] else None
    if n["doc_target"] and n["doc_target"] == victim:
        s["log"].append("Dokter selamatkan korban!")
        victim = None
    if victim:
        s["alive"].discard(victim); s["log"].append(f"{pname(r, victim)} dimangsa.")
    if _ww_check(r): return
    s["phase"] = "day_vote"
    s["log"].append(f"Siang {s['round']}: voting!")


def _ww_check(r):
    s = r["state"]
    w = [p for p in s["alive"] if s["roles"][p] == "werewolf"]
    v = [p for p in s["alive"] if s["roles"][p] != "werewolf"]
    if not w: s["winner"] = "village"; s["log"].append("Desa menang!")
    elif len(w) >= len(v): s["winner"] = "wolf"; s["log"].append("Werewolf menang!")
    if s["winner"] and not s["scored"]:
        s["scored"] = True
        if s["winner"] == "wolf": bump_win(r, [p for p in s["order"] if s["roles"][p] == "werewolf"])
        else: bump_win(r, [p for p in s["order"] if s["roles"][p] != "werewolf"])
    return bool(s["winner"])


def werewolf_view(r, uid):
    s = r["state"]; role = s["roles"].get(uid); n = s["night"]
    v = {"t": "state", "game": "werewolf", "phase": s["phase"],
         "order": s["order"], "names": {p: pname(r, p) for p in s["order"]},
         "alive": list(s["alive"]), "you": uid, "your_role": role,
         "log": s["log"][-12:], "winner": s["winner"], "round": s["round"],
         "day_votes": s["day_votes"] if s["phase"] == "day_vote" else {},
         "wins": r.get("wins", {})}
    if role == "werewolf":
        v["wolves"] = [p for p in s["alive"] if s["roles"][p] == "werewolf"]
        v["my_wolf_vote"] = n.get("wolf", {}).get(uid)
    if role == "seer":
        v["seer_results"] = s["seer_log"].get(uid, {})
        v["seer_acted"] = uid in n.get("seer_result", {})
        v["seer_last"] = n.get("seer_result", {}).get(uid)
    if role == "doctor":
        v["doc_last"] = s["doc_last"]
        v["doc_acted"] = n.get("doc_acted", False)
    return v


def werewolf_handle(r, uid, msg):
    s = r["state"]; t = msg.get("t")
    if s["winner"] or uid not in s["alive"]: return
    role = s["roles"][uid]
    if s["phase"] == "night":
        n = s["night"]
        if t == "kill" and role == "werewolf":
            tgt = msg.get("target")
            if tgt not in s["alive"] or s["roles"][tgt] == "werewolf":
                return {"error": "Target tidak valid."}
            n["wolf"][uid] = tgt
        elif t == "seer_check" and role == "seer":
            if uid in n["seer_result"]: return {"error": "Sudah cek."}
            tgt = msg.get("target")
            if tgt not in s["alive"]: return {"error": "Target tidak valid."}
            n["seer_result"][uid] = tgt
            res = "Werewolf" if s["roles"][tgt] == "werewolf" else "Warga"
            s["seer_log"].setdefault(uid, {})[tgt] = res
        elif t == "doc_save" and role == "doctor":
            if n["doc_acted"]: return {"error": "Sudah pakai save."}
            tgt = msg.get("target")
            if tgt is not None:
                if tgt not in s["alive"]: return {"error": "Target tidak valid."}
                if tgt == s["doc_last"]: return {"error": "Tidak bisa save orang sama 2x."}
            n["doc_acted"] = True; n["doc_target"] = tgt
            if tgt: s["doc_last"] = tgt
        else: return {"error": "Aksi tidak valid."}
        if _ww_check_night_done(r): _ww_resolve_night(r)
    elif s["phase"] == "day_vote":
        if t != "vote": return {"error": "Aksi tidak valid."}
        tgt = msg.get("target")
        if tgt not in s["alive"]: return {"error": "Target tidak valid."}
        s["day_votes"][uid] = tgt
        if all(v in s["day_votes"] for v in s["alive"]):
            c = Counter(s["day_votes"].values()); top = c.most_common()
            if len(top) > 1 and top[0][1] == top[1][1]:
                s["log"].append("Voting seri.")
            else:
                el = top[0][0]; s["alive"].discard(el)
                s["log"].append(f"{pname(r, el)} dieliminasi ({s['roles'][el]}).")
            if not _ww_check(r): _ww_night(r)


# ================= TRANSPORT =================
GAMES = {
    "ludo": {"start": ludo_start, "view": ludo_view, "handle": ludo_handle},
    "tod": {"start": tod_start, "view": tod_view, "handle": tod_handle},
    "gunung": {"start": gunung_start, "view": gunung_view, "handle": gunung_handle},
    "monyet": {"start": monyet_start, "view": monyet_view, "handle": monyet_handle},
    "werewolf": {"start": werewolf_start, "view": werewolf_view, "handle": werewolf_handle},
    "distrik": {"start": distrik_start, "view": dview, "handle": None},
}


async def broadcast_voice(r):
    peers = [{"id": p, "name": pname(r, p),
              "muted": r.get("voice_muted", {}).get(p, False)}
             for p in r.get("voice", set())]
    await dbroadcast(r, {"t": "voice_update", "peers": peers})


async def push_room(code):
    r = ROOMS.get(code)
    if not r: return
    v = {"t": "room", "code": code, "game": r["game"], "host": r["host"],
         "started": r["started"], "limits": LIMITS[r["game"]],
         "players": [{"id": i, "name": p["name"]} for i, p in r["players"].items()],
         "wins": r.get("wins", {})}
    for p in r["players"].values():
        if not p["ws"].closed:
            await p["ws"].send_json(v)


async def leave(code, uid, ws):
    r = ROOMS.get(code)
    if not r or uid not in r["players"] or r["players"][uid]["ws"] is not ws: return
    if r.get("voice") and uid in r["voice"]:
        r["voice"].discard(uid)
        r.get("voice_muted", {}).pop(uid, None)
        for pid in r["voice"]:
            await dsend(r, pid, {"t": "voice_peer_left", "uid": uid})
        await broadcast_voice(r)
    del r["players"][uid]
    if not r["players"]:
        if r.get("task"): r["task"].cancel()
        ROOMS.pop(code, None); return
    if r["host"] == uid: r["host"] = next(iter(r["players"]))
    if r["started"] and r["game"] != "distrik":
        eng = GAMES.get(r["game"])
        if eng and eng["view"] and r["state"]:
            for pid in r["players"]:
                try: await r["players"][pid]["ws"].send_json(eng["view"](r, pid))
                except Exception: pass
    await push_room(code)


async def ws_handler(req):
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=0)
    await ws.prepare(req)
    user = code = None
    try:
        async for m in ws:
            if m.type != WSMsgType.TEXT: continue
            try: msg = json.loads(m.data)
            except ValueError: continue
            t = msg.get("t")
            if t == "auth":
                user = verify(msg.get("initData", ""))
                if not user:
                    await ws.send_json({"t": "error", "m": "Buka lewat Telegram ya."}); break
                await ws.send_json({"t": "ready", "me": user["id"],
                                    "name": user.get("first_name", "Pemain")})
                continue
            if not user: continue
            uid, name = user["id"], user.get("first_name", "Pemain")

            if t == "create" and msg.get("game") in LIMITS:
                await leave(code, uid, ws)
                code = "".join(random.choices(string.ascii_uppercase, k=5))
                ROOMS[code] = {"game": msg["game"], "host": uid, "started": False,
                               "players": {uid: {"name": name, "ws": ws}},
                               "state": None, "task": None, "wins": {},
                               "voice": set(), "voice_muted": {}}
                await push_room(code)
                continue
            if t == "join":
                c = str(msg.get("code", "")).upper().strip()
                r = ROOMS.get(c)
                if not r:
                    await ws.send_json({"t": "error", "m": "Room tidak ditemukan."})
                elif r["started"] and uid not in r["players"]:
                    await ws.send_json({"t": "error", "m": "Game sudah dimulai."})
                elif uid not in r["players"] and len(r["players"]) >= LIMITS[r["game"]][1]:
                    await ws.send_json({"t": "error", "m": "Room penuh."})
                else:
                    if code and code != c: await leave(code, uid, ws)
                    code = c
                    r["players"][uid] = {"name": name, "ws": ws}
                    await push_room(c)
                    if r["started"] and r["state"]:
                        if r["game"] == "distrik":
                            await dsend(r, uid, dview(r, uid))
                        else:
                            await ws.send_json(GAMES[r["game"]]["view"](r, uid))
                    if r.get("voice"):
                        await broadcast_voice(r)
                continue
            r = ROOMS.get(code)
            if not r: continue

            if t == "voice_join":
                r.setdefault("voice", set()).add(uid)
                r.setdefault("voice_muted", {})[uid] = False
                existing = list(r["voice"] - {uid})
                await ws.send_json({"t": "voice_peer_list", "peers": existing})
                for pid in existing:
                    await dsend(r, pid, {"t": "voice_peer_joined", "uid": uid})
                await broadcast_voice(r)
                continue
            if t == "voice_leave":
                if r.get("voice") and uid in r["voice"]:
                    r["voice"].discard(uid)
                    r.get("voice_muted", {}).pop(uid, None)
                    await broadcast_voice(r)
                    for pid in r["voice"]:
                        await dsend(r, pid, {"t": "voice_peer_left", "uid": uid})
                continue
            if t == "voice_mute":
                if r.get("voice") and uid in r["voice"]:
                    r.setdefault("voice_muted", {})[uid] = bool(msg.get("muted"))
                    await broadcast_voice(r)
                continue
            if t == "rtc_signal":
                to = msg.get("to")
                if to:
                    await dsend(r, to, {"t": "rtc_signal", "from": uid,
                                        "kind": msg.get("kind"),
                                        "sdp": msg.get("sdp"),
                                        "candidate": msg.get("candidate")})
                continue

            if t == "start":
                if r["host"] == uid and not r["started"]:
                    lo, hi = LIMITS[r["game"]]
                    if lo <= len(r["players"]) <= hi:
                        r["started"] = True
                        GAMES[r["game"]]["start"](r)
                        await push_room(code)
                        if r["game"] != "distrik":
                            for pid in r["players"]:
                                try: await r["players"][pid]["ws"].send_json(GAMES[r["game"]]["view"](r, pid))
                                except Exception: pass
                    else:
                        await ws.send_json({"t": "error", "m": f"Butuh {lo}–{hi} pemain."})
                continue
            if t == "leave":
                if r.get("task"): r["task"].cancel()
                r["started"] = False; r["state"] = None
                await leave(code, uid, ws); code = None
                await ws.send_json({"t": "left"})
                continue
            if t == "restart":
                if r["host"] == uid and r["started"]:
                    if r.get("task"): r["task"].cancel()
                    r["started"] = False; r["state"] = None; r["task"] = None
                    await push_room(code)
                continue
            if r["game"] == "distrik" and r["started"] and r["state"]:
                q = r["state"]["queues"].get(uid)
                if q: q.put_nowait(msg)
            elif r["started"] and r["state"]:
                err = GAMES[r["game"]]["handle"](r, uid, msg) if GAMES[r["game"]]["handle"] else None
                if err and "error" in err:
                    await ws.send_json({"t": "error", "m": err["error"]})
                else:
                    for pid in r["players"]:
                        try: await r["players"][pid]["ws"].send_json(GAMES[r["game"]]["view"](r, pid))
                        except Exception: pass
    finally:
        if user and code:
            r = ROOMS.get(code)
            if not (r and r["started"]):
                await leave(code, user["id"], ws)
    return ws


async def index(_):
    return web.FileResponse(Path(__file__).parent / "static" / "index.html")


app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/ws", ws_handler)

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=8080)