"""Rough local mechfight simulator for A/B testing bots in 16-way free-for-alls.

Approximations (deliberate): no NPCs, crates, supply drops or mortar fire; mech radius is
fixed (no growth per cube); no mech-mech pushing; walls are sampled on the 1 m grid.
Everything bots see goes through the same obs format as the real server.
"""
import copy
import importlib
import json
import math
import random
import sys

import os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.dirname(HERE))  # repo root: bot.py, mechfight.py
import mechfight as mf  # noqa: E402

mf.log = lambda *a: None
MAPDIR = os.environ.get("MECH_ARENA", os.path.join(HERE, "..", "..", "mechfight-kit", "arena"))
HELLO = json.load(open(os.path.join(HERE, "hello.json")))
CFG = HELLO["config"]
DT = 1.0 / CFG["match"]["tick_hz"]
_MAP = None


def get_map():
    global _MAP
    if _MAP is None:
        import bot
        _MAP = bot.Map(MAPDIR)
    return _MAP


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def hyp(x, y):
    return math.sqrt(x * x + y * y)


class Mech:
    def __init__(self, i, pos, aim):
        mc = CFG["mech"]
        self.id = i
        self.stats = {k: v for k, v in mc.items()}
        self.wmods = {}  # kind -> {stat: (mul, add)}
        self.x, self.y, self.z = pos
        self.vx = self.vy = self.vz = 0.0
        self.aim = [aim, 0.0]
        self.cmd_aim = [aim, 0.0]
        self.hp = mc["hp"]
        self.energy = mc["energy"]
        self.energy_t = -99.0
        self.grounded = True
        self.cubes = 0
        self.upgrades = []
        self.weapons = [self.new_weapon("gun"), None]
        self.alive = True
        self.last_combat = -99.0
        self.act = {}
        self.events = []
        self.seen = {}
        self.offer = None
        self.offers_q = 0
        self.next_threshold = 0
        self.step_t = 0.0
        self.short_until = -99.0
        self.place = None
        self.kills = 0

    # -- stats
    def max_hp(self):
        return self.stats["hp"] * (1 + CFG["cubes"]["hp_per_cube"] * min(self.cubes, CFG["cubes"]["stat_cap"]))

    def wstats(self, kind):
        s = dict(CFG["weapons"][kind])
        for k, (mul, add) in self.wmods.get(kind, {}).items():
            if k in s:
                s[k] = s[k] * mul + add
        return s

    def new_weapon(self, kind):
        w = {"kind": kind, "cooldown": 0.0}
        if kind == "gun":
            w["ammo"] = self.wstats("gun")["mag"] if hasattr(self, "wmods") else CFG["weapons"]["gun"]["mag"]
            w["reload"] = 0.0
        if kind == "laser":
            w["charge"] = 0.0
        return w

    def dmg_mult(self):
        return 1 + CFG["cubes"]["damage_per_cube"] * min(self.cubes, CFG["cubes"]["stat_cap"])

    def has(self, kind):
        for i, w in enumerate(self.weapons):
            if w and w["kind"] == kind:
                return i
        return None


class Match:
    def __init__(self, bot_specs, seed):
        self.rng = random.Random(seed)
        self.M = get_map()
        self.t = 0.0
        self.tick = 0
        self.mechs = []
        self.bots = []
        self.bullets = []
        self.sounds = []  # (t, kind, x, y, by)
        self.pickups = {}
        self.cubes = {}
        self.next_id = 8000
        self.next_cube = 5000
        self.deaths = []
        self._zone_setup()
        spawns = self._spawns(len(bot_specs))
        for i, (spec, p) in enumerate(zip(bot_specs, spawns)):
            aim = math.atan2(self.zc[0][1] - p[1], self.zc[0][0] - p[0])
            m = Mech(i, p, aim)
            self.mechs.append(m)
            mod = importlib.import_module(spec)
            h = copy.deepcopy(HELLO)
            h["slot"] = i
            h["seed"] = self.rng.randrange(1 << 30)
            h["map"]["dir"] = MAPDIR
            mod.Map = lambda d: self.M  # share one raster
            b = mod.Bot(h)
            self.bots.append(b)
        self._spawn_loot()

    # ---- setup
    def _walkable(self, x, y):
        k = self.M.idx(x, y)
        return self.M.top[k] < self.M.ground[k] + 0.5

    def _zone_setup(self):
        z = CFG["zone"]
        c = list(z["start_center"])
        r = z["start_radius"]
        self.zc, self.zr, self.zt = [c], [r], []
        T = 0.0
        for ph in z["phases"]:
            nr = ph["radius"]
            for _ in range(200):
                a = self.rng.uniform(0, 2 * math.pi)
                d = self.rng.uniform(0, max(0.0, r - nr)) * 0.9
                nc = [c[0] + math.cos(a) * d, c[1] + math.sin(a) * d]
                if self.M.bx0 + nr * 0.3 < nc[0] < self.M.bx1 - nr * 0.3 and self.M.by0 + nr * 0.3 < nc[1] < self.M.by1 - nr * 0.3:
                    if nr > 30 or self._walkable(*nc):
                        break
            self.zt.append((T, T + ph["hold_s"], T + ph["hold_s"] + ph["shrink_s"], ph["dps"]))
            T += ph["hold_s"] + ph["shrink_s"]
            self.zc.append(nc)
            self.zr.append(nr)
            c, r = nc, nr

    def zone_obs(self):
        for i, (t0, s0, s1, dps) in enumerate(self.zt):
            if self.t < s1 or i == len(self.zt) - 1:
                return {"center": self.zc[i], "radius": self.zr[i], "next_center": self.zc[i + 1],
                        "next_radius": self.zr[i + 1], "shrink_start": s0, "shrink_end": s1, "dps": dps}

    def zone_now(self):
        z = self.zone_obs()
        if self.t <= z["shrink_start"]:
            return z["center"][0], z["center"][1], z["radius"], z["dps"]
        f = min(1.0, (self.t - z["shrink_start"]) / (z["shrink_end"] - z["shrink_start"]))
        c, n = z["center"], z["next_center"]
        return c[0] + (n[0] - c[0]) * f, c[1] + (n[1] - c[1]) * f, z["radius"] + (z["next_radius"] - z["radius"]) * f, z["dps"]

    def _spawns(self, n):
        out = []
        c = CFG["zone"]["start_center"]
        R = CFG["zone"]["start_radius"] * CFG["match"]["spawn_zone_fraction"]
        sep = CFG["match"]["spawn_min_separation"]
        tries = 0
        while len(out) < n:
            tries += 1
            if tries > 20000:
                sep *= 0.9
                tries = 0
            a = self.rng.uniform(0, 2 * math.pi)
            d = R * math.sqrt(self.rng.random())
            x, y = c[0] + math.cos(a) * d, c[1] + math.sin(a) * d
            if not (self.M.bx0 + 30 < x < self.M.bx1 - 30 and self.M.by0 + 30 < y < self.M.by1 - 30):
                continue
            if not self._walkable(x, y):
                continue
            if any(hyp(x - p[0], y - p[1]) < sep for p in out):
                continue
            out.append((x, y, self.M.gz(x, y)))
        return out

    def _spawn_loot(self):
        kinds = ["mortar", "laser", "lightning"]
        n = 0
        while n < CFG["loot"]["floor_count"]:
            x = self.rng.uniform(self.M.bx0 + 30, self.M.bx1 - 30)
            y = self.rng.uniform(self.M.by0 + 30, self.M.by1 - 30)
            if not self._walkable(x, y):
                continue
            self.add_pickup(self.rng.choice(kinds), x, y, self.M.gz(x, y))
            n += 1

    def add_pickup(self, kind, x, y, z):
        self.pickups[self.next_id] = {"id": self.next_id, "kind": kind, "pos": [x, y, z]}
        self.next_id += 1

    # ---- observation
    def visible(self, a, b):
        dx, dy = b.x - a.x, b.y - a.y
        d = hyp(dx, dy)
        near = CFG["vision"]["near_range"] + a.stats.get("near_range_bonus", 0)
        rng = CFG["vision"]["range"] + a.stats.get("vision_bonus", 0)
        if d > rng:
            return False
        if d > near:
            if abs(wrap(math.atan2(dy, dx) - a.aim[0])) > math.radians(CFG["vision"]["fov_deg"]) / 2:
                return False
        ez = a.z + a.stats["eye_height"]
        return self.M.los(a.x, a.y, ez, b.x, b.y, b.z + 4.5, 2.0) or self.M.los(a.x, a.y, ez, b.x, b.y, b.z + 8.5, 2.0)

    def see_point(self, a, x, y, z):
        dx, dy = x - a.x, y - a.y
        d = hyp(dx, dy)
        if d > CFG["vision"]["range"]:
            return False
        if d > CFG["vision"]["near_range"] and abs(wrap(math.atan2(dy, dx) - a.aim[0])) > math.radians(CFG["vision"]["fov_deg"]) / 2:
            return False
        return self.M.los(a.x, a.y, a.z + 8, x, y, z + 1, 3.0)

    def weapon_obs(self, m, w):
        if w is None:
            return None
        o = dict(w)
        o["stats"] = m.wstats(w["kind"])
        return o

    def obs(self, m):
        mechs = []
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            if self.visible(m, o):
                m.seen[o.id] = self.t
            if self.t - m.seen.get(o.id, -99) <= CFG["vision"]["memory_s"]:
                ws = [w["kind"] for w in o.weapons if w]
                charging = any(w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in o.weapons)
                mechs.append({"id": o.id, "kind": "mech", "pos": [o.x, o.y, o.z], "vel": [o.vx, o.vy, o.vz],
                              "aim": list(o.aim), "hp": o.hp, "max_hp": o.max_hp(), "cubes": o.cubes,
                              "weapons": ws, "charging": charging})
        pickups = [p for p in self.pickups.values() if hyp(p["pos"][0] - m.x, p["pos"][1] - m.y) < 200 and self.see_point(m, *p["pos"])]
        cubes = [c for c in self.cubes.values() if hyp(c["pos"][0] - m.x, c["pos"][1] - m.y) < 200 and self.see_point(m, *c["pos"])]
        sounds = []
        for (st, kind, sx, sy, by) in self.sounds:
            if by == m.id or self.t - st > 0.1 + 1e-6:
                continue
            d = hyp(sx - m.x, sy - m.y)
            if d > CFG["sound"]["range"].get(kind, 0):
                continue
            noise = math.radians(CFG["sound"]["bearing_noise_deg"]) * m.stats.get("hearing", 1)
            sounds.append({"kind": kind, "bearing": wrap(math.atan2(sy - m.y, sx - m.x) + self.rng.gauss(0, noise)),
                           "dist": "near" if d < 60 else ("mid" if d < 160 else "far"), "age": round(self.t - st, 2)})
        cx, cy, cr, dps = self.zone_now()
        s = dict(m.stats)
        s["muzzle_height"] = s.get("muzzle_height", 7)
        ev, m.events = m.events, []
        return {"type": "obs", "tick": self.tick, "t": round(self.t, 2), "alive": sum(1 for o in self.mechs if o.alive),
                "self": {"id": m.id, "pos": [m.x, m.y, m.z], "vel": [m.vx, m.vy, m.vz], "aim": list(m.aim),
                         "hp": m.hp, "max_hp": m.max_hp(), "energy": m.energy, "max_energy": m.stats["energy"],
                         "grounded": m.grounded, "cubes": m.cubes, "upgrades": list(m.upgrades),
                         "in_zone": hyp(m.x - cx, m.y - cy) <= cr,
                         "weapons": [self.weapon_obs(m, w) for w in m.weapons], "stats": s},
                "mechs": mechs, "projectiles": [], "crates": [], "cubes": cubes, "pickups": pickups,
                "sounds": sounds, "beams": [], "events": ev, "drops": [], "zone": self.zone_obs(), "offer": m.offer}

    # ---- actions
    def apply_action(self, m, a):
        m.act = a
        if "aim" in a:
            m.cmd_aim = [float(a["aim"][0]), max(m.stats["min_pitch"], min(m.stats["max_pitch"], float(a["aim"][1])))]
        if a.get("pick") is not None and m.offer:
            i = int(a["pick"])
            if 0 <= i < len(m.offer["choices"]):
                self.apply_upgrade(m, m.offer["choices"][i]["id"])
        if a.get("reload") and m.weapons[0] is not None:
            for w in m.weapons:
                if w and w["kind"] == "gun" and w["reload"] <= 0 and w["ammo"] < m.wstats("gun")["mag"]:
                    w["reload"] = m.wstats("gun")["reload"]
        if a.get("take") is not None:
            self.take(m, a["take"], a.get("drop"))
        elif a.get("drop") is not None:
            pass

    def take(self, m, pid, drop):
        p = self.pickups.get(pid)
        if p is None:
            m.events.append({"type": "take_failed", "id": pid, "reason": "gone"})
            return
        if hyp(p["pos"][0] - m.x, p["pos"][1] - m.y) > CFG["loot"]["pickup_radius"] or abs(p["pos"][2] - m.z) > 4:
            m.events.append({"type": "take_failed", "id": pid, "reason": "range"})
            return
        if m.has(p["kind"]) is not None:
            m.events.append({"type": "take_failed", "id": pid, "reason": "have"})
            return
        slot = None
        for i, w in enumerate(m.weapons):
            if w is None:
                slot = i
                break
        if slot is None:
            if drop is None:
                m.events.append({"type": "take_failed", "id": pid, "reason": "full"})
                return
            slot = int(drop)
            old = m.weapons[slot]
            if old["kind"] == "laser" and old.get("charge", 0) > 0:
                m.events.append({"type": "take_failed", "id": pid, "reason": "busy"})
                return
            self.add_pickup(old["kind"], m.x, m.y, m.z)
        del self.pickups[pid]
        w = m.new_weapon(p["kind"])
        w["cooldown"] = max(CFG["loot"]["equip_s"], m.wstats(p["kind"]).get("cooldown", 0))
        m.weapons[slot] = w
        m.events.append({"type": "took", "id": pid, "kind": p["kind"], "slot": slot})

    def apply_upgrade(self, m, uid):
        u = next(u for u in CFG["upgrades"] if u["id"] == uid)
        old_max = m.max_hp()
        for e in u["effects"]:
            root, *rest = e["path"].split(".")
            if root == "mech":
                k = rest[0]
                if "mul" in e:
                    m.stats[k] = m.stats.get(k, 0) * e["mul"]
                if "add" in e:
                    m.stats[k] = m.stats.get(k, 0) + e["add"]
            else:
                kind, k = rest
                mul, add = m.wmods.setdefault(kind, {}).get(k, (1.0, 0.0))
                m.wmods[kind][k] = (mul * e.get("mul", 1.0), add + e.get("add", 0.0))
        m.hp += max(0.0, m.max_hp() - old_max)
        m.upgrades.append(uid)
        m.events.append({"type": "upgrade", "id": uid})
        m.offer = None
        self.maybe_offer(m)

    def maybe_offer(self, m):
        th = CFG["cubes"]["upgrade_thresholds"]
        while m.next_threshold < len(th) and m.cubes >= th[m.next_threshold]:
            m.offers_q += 1
            m.next_threshold += 1
        if m.offer is None and m.offers_q > 0:
            m.offers_q -= 1
            held = [w["kind"] for w in m.weapons if w]
            ok = [u for u in CFG["upgrades"] if m.upgrades.count(u["id"]) < u["max"] and (not u.get("requires") or u["requires"] in held)]
            ch = self.rng.sample(ok, min(3, len(ok)))
            if ch:
                m.offer = {"id": m.next_threshold, "expires": self.t + CFG["bot"]["offer_timeout_s"],
                           "choices": [{"id": u["id"], "name": u["name"], "desc": u["desc"]} for u in ch]}

    # ---- damage
    def damage(self, tgt, src, amount, weapon, from_x, from_y):
        if not tgt.alive:
            return
        rear = False
        if weapon != "zone":
            b = math.atan2(from_y - tgt.y, from_x - tgt.x)
            if abs(wrap(b - tgt.aim[0])) > math.radians(tgt.stats["front_arc_deg"]) / 2:
                amount *= tgt.stats["rear_mult"]
                rear = True
            amount *= max(0.0, 1 - tgt.stats.get("armor", 0))
            tgt.last_combat = self.t
            if src is not None:
                src.last_combat = self.t
        tgt.hp -= amount
        ev = {"type": "damaged", "amount": round(amount, 2), "weapon": weapon,
              "bearing": None if weapon == "zone" else math.atan2(from_y - tgt.y, from_x - tgt.x),
              "by": None if src is None else src.id}
        if rear:
            ev["rear"] = True
        tgt.events.append(ev)
        if src is not None:
            src.events.append({"type": "hit", "target": tgt.id, "amount": round(amount, 2), "weapon": weapon})
        if tgt.hp <= 0:
            self.kill(tgt, src)

    def kill(self, m, src):
        m.alive = False
        m.hp = 0
        self.deaths.append((self.tick, m.id))
        self.sounds.append((self.t, "death", m.x, m.y, m.id))
        for w in m.weapons:
            if w and w["kind"] != "gun":
                self.add_pickup(w["kind"], m.x + self.rng.uniform(-2, 2), m.y + self.rng.uniform(-2, 2), m.z)
        for _ in range(m.cubes + CFG["cubes"]["death_drop_extra"]):
            self.cubes[self.next_cube] = {"id": self.next_cube, "pos": [m.x + self.rng.uniform(-3, 3), m.y + self.rng.uniform(-3, 3), m.z]}
            self.next_cube += 1
        if src is not None:
            src.kills += 1
            src.events.append({"type": "kill", "target": m.id})

    # ---- weapons
    def fire(self, m):
        fire = m.act.get("fire") or [False, False]
        yaw, pitch = m.aim
        dx, dy, dz = math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch)
        mx, my, mz = m.x + dx * 3, m.y + dy * 3, m.z + m.stats["muzzle_height"] + dz * 3
        for i, w in enumerate(m.weapons):
            if w is None:
                continue
            held = bool(fire[i]) if i < len(fire) else False
            st = m.wstats(w["kind"])
            k = w["kind"]
            if k == "gun":
                if held and w["cooldown"] <= 0 and w["reload"] <= 0 and w["ammo"] > 0:
                    sp = math.radians(st["spread_deg"])
                    y2, p2 = yaw + self.rng.gauss(0, sp), pitch + self.rng.gauss(0, sp)
                    v = st["speed"]
                    self.bullets.append([mx, my, mz, math.cos(p2) * math.cos(y2) * v, math.cos(p2) * math.sin(y2) * v,
                                         math.sin(p2) * v, st["range"], m, st["damage"] * m.dmg_mult()])
                    w["ammo"] -= 1
                    w["cooldown"] = st["cooldown"]
                    self.sounds.append((self.t, "gun", m.x, m.y, m.id))
                    if w["ammo"] <= 0:
                        w["reload"] = st["reload"]
            elif k == "laser":
                if self.t < m.short_until:
                    w["charge"] = 0.0
                    continue
                if held and w["cooldown"] <= 0:
                    if w["charge"] == 0:
                        self.sounds.append((self.t, "laser_charge", m.x, m.y, m.id))
                    w["charge"] += DT
                    if w["charge"] >= st["charge"] + st["hold_s"]:
                        self.laser(m, w, st, mx, my, mz, dx, dy, dz)
                elif w["charge"] > 0:
                    if w["charge"] < st["min_charge"]:
                        w["charge"] += DT
                    else:
                        self.laser(m, w, st, mx, my, mz, dx, dy, dz)
            elif k == "lightning":
                if held and w["cooldown"] <= 0 and m.energy >= st["energy"]:
                    m.energy -= st["energy"]
                    m.energy_t = self.t
                    w["cooldown"] = st["cooldown"]
                    self.sounds.append((self.t, "lightning", m.x, m.y, m.id))
                    self.lightning(m, st)

    def laser(self, m, w, st, mx, my, mz, dx, dy, dz):
        frac = st["min_frac"] + (1 - st["min_frac"]) * min(1.0, w["charge"] / st["charge"])
        w["charge"] = 0.0
        w["cooldown"] = st["cooldown"]
        self.sounds.append((self.t, "laser", m.x, m.y, m.id))
        hd = hyp(dx, dy)
        best = None
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            ox, oy = o.x - mx, o.y - my
            if hd < 1e-6:
                continue
            tt = (ox * dx + oy * dy) / (hd * hd)  # param along full 3D dir, by horizontal projection
            if tt <= 0 or tt > st["range"]:
                continue
            px, py = mx + dx * tt, my + dy * tt
            if hyp(o.x - px, o.y - py) > o.stats["radius"]:
                continue
            pz = mz + dz * tt
            if not (o.z <= pz <= o.z + o.stats["height"]):
                continue
            if best is None or tt < best[0]:
                best = (tt, o, pz)
        if best is not None:
            tt, o, pz = best
            if self.M.los(mx, my, mz, o.x, o.y, pz, 1.5):
                mult = 1 + (st.get("cookoff", 0) if o.has("mortar") is not None else 0)
                self.damage(o, m, st["damage"] * frac * m.dmg_mult() * mult, "laser", m.x, m.y)

    def lightning(self, m, st):
        cone = math.radians(st["cone_deg"])
        hit = []
        cand = None
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            d = hyp(o.x - m.x, o.y - m.y)
            if d > st["range"]:
                continue
            if abs(wrap(math.atan2(o.y - m.y, o.x - m.x) - m.aim[0])) > cone:
                continue
            if not self.M.los(m.x, m.y, m.z + 7, o.x, o.y, o.z + 4.5, 1.5):
                continue
            if cand is None or d < cand[0]:
                cand = (d, o)
        if cand is None:
            return
        cur, dmg = cand[1], st["damage"] * m.dmg_mult()
        for _ in range(int(st["chain"]) + 1):
            hit.append(cur)
            self.damage(cur, m, dmg, "lightning", m.x, m.y)
            if cur.has("laser") is not None:
                cur.short_until = self.t + st["short_s"]
                li = cur.has("laser")
                cur.weapons[li]["charge"] = 0.0
            nxt = None
            for o in self.mechs:
                if o is m or not o.alive or o in hit:
                    continue
                d = hyp(o.x - cur.x, o.y - cur.y)
                if d <= st["chain_range"] and (nxt is None or d < nxt[0]):
                    nxt = (d, o)
            if nxt is None:
                break
            cur = nxt[1]
            dmg *= st["chain_falloff"]

    def step_bullets(self):
        keep = []
        for b in self.bullets:
            x, y, z, vx, vy, vz, rng, src, dmg = b
            nx, ny, nz = x + vx * DT, y + vy * DT, z + vz * DT
            seg = math.sqrt((vx * DT) ** 2 + (vy * DT) ** 2 + (vz * DT) ** 2)
            hitm = None
            best = 1e9
            for o in self.mechs:
                if o is src or not o.alive:
                    continue
                if abs(o.x - (x + nx) / 2) > seg + 4 or abs(o.y - (y + ny) / 2) > seg + 4:
                    continue
                # closest approach in 2D of segment to cylinder axis
                sx, sy = nx - x, ny - y
                L2 = sx * sx + sy * sy
                tt = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, ((o.x - x) * sx + (o.y - y) * sy) / L2))
                px, py, pz = x + sx * tt, y + sy * tt, z + (nz - z) * tt
                if hyp(o.x - px, o.y - py) <= o.stats["radius"] + 0.3 and o.z - 0.3 <= pz <= o.z + o.stats["height"] + 0.3:
                    if tt < best:
                        best, hitm = tt, o
            if hitm is not None:
                hx, hy, hz = x + (nx - x) * best, y + (ny - y) * best, z + (nz - z) * best
                if self.M.los(x, y, z, hx, hy, hz, 1.5):
                    self.damage(hitm, src, dmg, "gun", x, y)
                    continue
            if not self.M.los(x, y, z, nx, ny, nz, 1.5):
                continue
            rng -= seg
            if rng <= 0:
                continue
            keep.append([nx, ny, nz, vx, vy, vz, rng, src, dmg])
        self.bullets = keep

    # ---- physics
    def solid(self, x, y, z, h):
        M = self.M
        if x < M.bx0 or x > M.bx1 or y < M.by0 or y > M.by1:
            return True
        k = M.idx(x, y)
        if M.top[k] > z + 1.2 and M.base[k] < z + h:
            return True
        return M.ground[k] > z + 1.2

    def blocked_at(self, m, x, y):
        r = m.stats["radius"] * 0.8
        h = m.stats["height"]
        for ox, oy in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r)):
            if self.solid(x + ox, y + oy, m.z, h):
                return True
        return False

    def physics(self, m):
        a = m.act
        st = m.stats
        mv = a.get("move") or [0, 0]
        mx, my = float(mv[0]), float(mv[1])
        L = hyp(mx, my)
        if L > 1:
            mx, my = mx / L, my / L
        boost = bool(a.get("boost"))
        charging = any(w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in m.weapons)
        spd = st["walk_speed"]
        spent = False
        if boost and m.grounded and (mx or my) and m.energy > 0:
            spd = st["boost_speed"]
            m.energy -= st["boost_drain"] * DT
            spent = True
        if charging:
            spd *= st["charge_move_mult"]
        tvx, tvy = mx * spd, my * spd
        acc = (st["ground_accel"] if m.grounded else st["air_accel"]) * DT
        dvx, dvy = tvx - m.vx, tvy - m.vy
        dl = hyp(dvx, dvy)
        if dl > acc:
            dvx, dvy = dvx / dl * acc, dvy / dl * acc
        m.vx += dvx
        m.vy += dvy
        if a.get("jump") and m.grounded:
            m.vz = st["jump_speed"]
            m.grounded = False
        if not m.grounded:
            if boost and m.energy > 0:
                m.vz = min(m.vz + st["jet_accel"] * DT, st["max_rise_speed"])
                m.energy -= st["jet_drain"] * DT
                spent = True
                self.sounds.append((self.t, "jet", m.x, m.y, m.id)) if self.tick % 6 == 0 else None
            m.vz -= st["gravity"] * DT
        if spent:
            m.energy_t = self.t
            m.energy = max(0.0, m.energy)
        elif self.t - m.energy_t > st["energy_regen_delay"]:
            m.energy = min(st["energy"], m.energy + st["energy_regen"] * DT)
        # horizontal with sliding
        nx, ny = m.x + m.vx * DT, m.y + m.vy * DT
        if not self.blocked_at(m, nx, ny):
            m.x, m.y = nx, ny
        elif not self.blocked_at(m, nx, m.y):
            m.x = nx
            m.vy = 0.0
        elif not self.blocked_at(m, m.x, ny):
            m.y = ny
            m.vx = 0.0
        else:
            m.vx = m.vy = 0.0
        # vertical
        surf = self.M.surface(m.x, m.y, m.z + 1.2)
        if m.grounded:
            if surf < m.z - 0.6:
                m.grounded = False
            else:
                m.z = surf
        if not m.grounded:
            m.z += m.vz * DT
            if m.z <= surf and m.vz <= 0:
                m.z = surf
                m.vz = 0.0
                m.grounded = True
        # aim slew
        ty, tp = m.cmd_aim
        dyaw = wrap(ty - m.aim[0])
        mr = st["turn_rate"] * DT
        m.aim[0] = wrap(m.aim[0] + max(-mr, min(mr, dyaw)))
        pr = st["pitch_rate"] * DT
        m.aim[1] += max(-pr, min(pr, tp - m.aim[1]))
        # sounds of movement
        sp = hyp(m.vx, m.vy)
        if m.grounded and sp > CFG["sound"]["step_speed"] and self.t - m.step_t >= CFG["sound"]["step_every"]:
            m.step_t = self.t
            self.sounds.append((self.t, "boost" if spent else "step", m.x, m.y, m.id))

    def timers(self, m):
        for w in m.weapons:
            if not w:
                continue
            if w["cooldown"] > 0:
                w["cooldown"] = max(0.0, w["cooldown"] - DT)
            if w["kind"] == "gun" and w["reload"] > 0:
                w["reload"] -= DT
                if w["reload"] <= 0:
                    w["reload"] = 0.0
                    w["ammo"] = int(m.wstats("gun")["mag"])
        cx, cy, cr, dps = self.zone_now()
        if hyp(m.x - cx, m.y - cy) > cr:
            m.last_combat = self.t
            self.damage(m, None, dps * DT, "zone", m.x, m.y)
        elif self.t - m.last_combat > m.stats["regen_delay"]:
            m.hp = min(m.max_hp(), m.hp + m.stats["regen_per_s"] * DT)
        if m.offer and self.t >= m.offer["expires"]:
            self.apply_upgrade(m, m.offer["choices"][0]["id"])
        # cubes
        for cid in list(self.cubes):
            c = self.cubes[cid]["pos"]
            if hyp(c[0] - m.x, c[1] - m.y) <= CFG["cubes"]["pickup_radius"] and abs(c[2] - m.z) < 4:
                del self.cubes[cid]
                old = m.max_hp()
                m.cubes += 1
                m.hp += m.max_hp() - old
                m.events.append({"type": "pickup", "cubes": m.cubes})
                self.maybe_offer(m)

    # ---- run
    def run(self):
        dur = CFG["match"]["duration_s"]
        every = CFG["match"]["decide_every"]
        while True:
            alive = [m for m in self.mechs if m.alive]
            if len(alive) <= 1 or self.t >= dur:
                break
            if self.tick % every == 0:
                for m, b in zip(self.mechs, self.bots):
                    if not m.alive:
                        continue
                    o = self.obs(m)
                    try:
                        a = b.act(o) or {}
                    except Exception as e:  # a crashing bot idles, as on the server
                        a = {}
                        if not getattr(b, "_crashed", False):
                            b._crashed = True
                            print("bot crash", type(b).__module__, repr(e), file=sys.stderr)
                    self.apply_action(m, a)
                self.sounds = [s for s in self.sounds if self.t - s[0] <= 0.1 + 1e-6]
            for m in self.mechs:
                if m.alive:
                    self.physics(m)
            for m in self.mechs:
                if m.alive:
                    self.fire(m)
            self.step_bullets()
            for m in self.mechs:
                if m.alive:
                    self.timers(m)
            self.tick += 1
            self.t = self.tick * DT
        # placements
        n = len(self.mechs)
        place = {}
        order = sorted(self.deaths)  # earliest first
        survivors = sorted([m for m in self.mechs if m.alive], key=lambda m: -m.hp)
        # survivors: 1..k by hp
        for i, m in enumerate(survivors):
            place[m.id] = i + 1
        # deaths: latest death gets place len(survivors)+1; same tick shares
        by_tick = {}
        for tk, mid in order:
            by_tick.setdefault(tk, []).append(mid)
        nxt = len(survivors) + 1
        for tk in sorted(by_tick, reverse=True):
            ids = by_tick[tk]
            for mid in ids:
                place[mid] = nxt + len(ids) - 1  # mechs dying together share the worse place
            nxt += len(ids)
        return place


def run_match(args):
    specs, seed = args
    m = Match(specs, seed)
    place = m.run()
    res = []
    for mech, spec in zip(m.mechs, specs):
        res.append((spec, place[mech.id], mech.kills, round(m.t, 1)))
    return res


if __name__ == "__main__":
    import time
    t0 = time.time()
    specs = ["bot_v1"] * 6 + ["bot"] * 6 + ["bot_starter"] * 4
    r = run_match((specs, 1))
    for row in sorted(r, key=lambda z: z[1]):
        print(row)
    print("wall", time.time() - t0)
