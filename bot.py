"""vmech: a mechfight/1 bot.

Design: everything heavy (map raster) happens once at hello; each tick is cheap lookups.
Per tick: update tracks -> pick mode (zone / fight / retreat / loot / hold) -> move with
wall-aware steering -> aim with lead -> gate every trigger on aim error and line of sight.
"""
import gc
import json
import math
import os
import random
import sys
import time
from array import array

import mechfight as mf

TAU = 2 * math.pi
PI = math.pi


def wrap(a):
    return (a + PI) % TAU - PI


def hyp(x, y):
    return math.sqrt(x * x + y * y)


# ---------------------------------------------------------------- map
class Map:
    def __init__(self, d):
        w = json.load(open(os.path.join(d, "v2", "world.json")))
        t = w["terrain"]
        self.nx, self.ny = int(t["nx"]), int(t["ny"])
        self.x0, self.y0 = float(t["minX"]), float(t["minY"])
        self.step = float(t.get("step", 1.0))
        self.inv = 1.0 / self.step
        scale = float(t.get("scale", 0.01))
        raw = open(os.path.join(d, "v2", t.get("file", "terrain.i16")), "rb").read()
        g16 = array("h")
        g16.frombytes(raw[: self.nx * self.ny * 2])
        if sys.byteorder != "little":
            g16.byteswap()
        n = self.nx * self.ny
        self.ground = array("f", [v * scale for v in g16])
        self.top = array("f", [-1e9]) * n
        self.base = array("f", [1e9]) * n
        b = w["bounds"]
        self.bx0, self.bx1, self.by0, self.by1 = b["minX"] + 3, b["maxX"] - 3, b["minY"] + 3, b["maxY"] - 3
        for p in w["prisms"]:
            self._raster(p)

    def _raster(self, p):
        rings = [p["ring"]] + list(p.get("holes") or [])
        edges = []
        ymin, ymax = 1e18, -1e18
        for r in rings:
            m = len(r) // 2
            for k in range(m):
                x1, y1 = r[2 * k], r[2 * k + 1]
                x2, y2 = r[2 * ((k + 1) % m)], r[2 * ((k + 1) % m) + 1]
                if y1 != y2:
                    edges.append((x1, y1, x2, y2))
                ymin, ymax = min(ymin, y1), max(ymax, y1)
        if not edges:
            return
        top, base = float(p["top"]), float(p["base"])
        nx, x0, y0, inv, st = self.nx, self.x0, self.y0, self.inv, self.step
        j0 = max(0, int(math.floor((ymin - y0) * inv)) - 1)
        j1 = min(self.ny - 1, int(math.ceil((ymax - y0) * inv)) + 1)
        T, B = self.top, self.base
        for j in range(j0, j1 + 1):
            y = y0 + j * st
            xs = []
            for (x1, y1, x2, y2) in edges:
                if (y1 <= y < y2) or (y2 <= y < y1):
                    xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
            if len(xs) < 2:
                continue
            xs.sort()
            row = j * nx
            for q in range(0, len(xs) - 1, 2):
                # pad by half a cell so thin walls are never missed
                i0 = max(0, int(math.floor((xs[q] - x0) * inv)))
                i1 = min(nx - 1, int(math.ceil((xs[q + 1] - x0) * inv)))
                for i in range(i0, i1 + 1):
                    k = row + i
                    if top > T[k]:
                        T[k] = top
                    if base < B[k]:
                        B[k] = base

    def idx(self, x, y):
        i = int((x - self.x0) * self.inv + 0.5)
        j = int((y - self.y0) * self.inv + 0.5)
        if i < 0:
            i = 0
        elif i >= self.nx:
            i = self.nx - 1
        if j < 0:
            j = 0
        elif j >= self.ny:
            j = self.ny - 1
        return j * self.nx + i

    def gz(self, x, y):
        return self.ground[self.idx(x, y)]

    def surface(self, x, y, below):
        """Highest standing surface at (x,y) not above `below`."""
        k = self.idx(x, y)
        g = self.ground[k]
        t = self.top[k]
        if t > g and t <= below + 0.5:
            return t
        return g

    def los(self, ax, ay, az, bx, by, bz, step=1.5):
        dx, dy, dz = bx - ax, by - ay, bz - az
        L = hyp(dx, dy)
        n = int(L / step)
        if n < 2:
            return True
        G, T, B = self.ground, self.top, self.base
        nx, x0, y0, inv = self.nx, self.x0, self.y0, self.inv
        fx, fy, fz = dx / n, dy / n, dz / n
        x, y, z = ax, ay, az
        maxi, maxj = nx - 1, self.ny - 1
        for _ in range(n - 1):
            x += fx
            y += fy
            z += fz
            i = int((x - x0) * inv + 0.5)
            j = int((y - y0) * inv + 0.5)
            if i < 0 or j < 0 or i > maxi or j > maxj:
                continue
            k = j * nx + i
            if G[k] > z or (B[k] < z < T[k]):
                return False
        return True

    def blocked(self, x, y, fz, d=1.0):
        if x < self.bx0 or x > self.bx1 or y < self.by0 or y > self.by1:
            return True
        k = self.idx(x, y)
        t = self.top[k]
        if t > fz + 1.3 and self.base[k] < fz + 9.0:
            return True
        if self.ground[k] > fz + 1.3 + 1.1 * d:
            return True
        return False


# ---------------------------------------------------------------- helpers
UPGRADE_PRIORITY = [
    "armor", "nanites", "plating", "laser_damage", "gun_damage", "laser_charge",
    "gun_rate", "lightning_efficiency", "gyros", "gun_velocity", "capacitor",
    "reactor", "sensors", "servos", "gun_mag", "jets", "lightning_range",
    "lightning_chain", "laser_pierce", "mortar_reload", "mortar_splash", "mortar_velocity",
]
# practice duels reward killing faster; switched off once a free-for-all is detected
UPGRADE_PRIORITY_PRACTICE = [
    "gun_damage", "gun_mag", "laser_damage", "gun_rate", "laser_charge", "armor", "lightning_efficiency",
    "nanites", "plating", "gun_velocity", "gyros", "capacitor", "reactor", "sensors",
    "servos", "jets", "lightning_range", "lightning_chain", "laser_pierce", "mortar_reload",
    "mortar_splash", "mortar_velocity",
]
OFFENSE_IN_PRACTICE = 1.0
LIGHTNING_PRACTICE = 0.0  # practice: lightning > laser, look for it, wider fire cone
WEAPON_VALUE = {"laser": 3, "lightning": 2, "mortar": 0, "gun": 0}

# tunables (tested in the local simulator)
ENGAGE_CLOSE = 60.0
ENGAGE_CLOSE_LATE = 40.0
AIMED_RANGE = 160.0
WEAK_FRAC = 0.25
HOLD_RING = 0.7
RETREAT_HP = 0.45
AVOID_RANGE = 150.0
FAR_HOT = 1e9        # a mech hitting us from beyond this range is avoided, not fought, unless we are clearly ahead
AVOID_SHOOT = 0.0
PREF_RANGE = 60.0
PREF_RANGE_FFA = 60.0  # range once a death we didn't cause shows this is a free-for-all
PRACTICE_ENGAGE = 0.0   # 1: until FFA is detected, fight every visible mech within AIMED_RANGE (all are hunters)
WEAPON_HUNT = 0.0       # 1: with no second weapon, value weapon pickups much higher and look farther
STRAFE_MIN = 0.2
STRAFE_MAX = 0.5
FLIP_P = 0.8
AIM_TOL = 3.0
FIGHT_BOOST_E = 45.0
STRAFE_W = 0.95
RELOAD_COVER = 0.0  # 1: break line of sight while the gun reloads (and the laser is not ready)
DROP_RUSH = 0.0      # 1: supply drops (3 cubes + full HP/energy) come before other loot, from farther away
LOOT_SCAN = 0.0      # 1: spin the torso while unarmed (no second weapon) early on, to spot pickups
MRM = 1.0            # 1: Minimum Risk Movement while holding (Robocode melee): keep moving to the safest nearby spot
MRM_PERIOD = 1.0
LASER_SNAP = 1.0     # 1: once the laser is >=80% charged, aim at its hitscan point (not the gun lead) so it releases
FINISH_BONUS = 1.0   # practice: extra target priority for a visible hunter under 35% HP
RETREAT_HOLD = 0.0   # 1: hold fire while retreating (still facing the attacker) so the regen timer can start
OUTNUM_HP = 0.5      # retreat when outnumbered below this HP fraction
RETREAT_LASER = 0.0  # 1: may charge the laser while retreating (halves speed)
COVER_THREATS = 2    # how many nearby threats cover must hide from
HUNT_LONE = 1.0      # hunt mechs that are alone, keep moving until one is found; never turn away from a shooter
HUNT_HP = 0.60
PICK_FIGHTS = 0.0    # practice: attack a hunter only if it is alone and we are above PICK_HP; else heal out of sight
PICK_HP = 0.80
HEAL_FIRST = 0.0     # practice: below HEAL_HP and not under fire, break line of sight and regen before the next hunter
HEAL_HP = 0.85
FLEE_FFA = 0.0       # once a free-for-all is detected: run from anyone approaching, fight only when cornered
HIDE = 0.0           # hide until the end game: fight back only at close range, avoid everyone else
HIDE_UNTIL_T = 380.0
HIDE_UNTIL_ALIVE = 3
DODGE = 1.0          # dodge incoming bullets (closest approach < radius within 0.7 s) and laser charges aimed at us
DODGE_BOOST = 25.0   # boost while dodging if energy above this (0: off)
LIGHT_CONE = 0.4     # lightning fires when aim is within cone_deg * this of the target (cone_deg is the full width, so 0.5 = edge)
SNAP_FRAC = 0.8      # LASER_SNAP: snap aim to the hitscan point from this charge fraction
MATCHUP = 0.0       # target-score bonus per edge: back turned to us, laser vs mortar carrier, lightning vs laser carrier < 45 m
STUCK_FIX = 1.0      # no stuck-jet from low net displacement while strafing in a fight (only when steering is blocked)
HIGH_AIM = 1.0       # if the waist is behind cover, aim at the upper body when that has line of sight
SOFT_WAIT = 0.0      # don't shoot NPCs/crates within 3 s of taking damage (turn to the shooter instead)
NO_NPC_LASER = 0.0   # never charge the laser on NPC drones
FINISH_FIRST = 1.0   # don't take the outnumbered retreat while a visible target is under 260 HP (and we are > 25%)
TOPOFF = 1.0         # reload a partial mag when no enemy is known
RETREAT_DUMP = 1.0   # release a laser charge right away when retreating
ISOLATE = 1.0        # with 2+ hunters in view, strafe to the side that breaks line of sight to the others and keeps it on the target
ISO_STEP = 10.0
COVER_FIGHT = 0.0    # anchor fights near cover corners, isolate 1v2+ angles, duck enemy lasers and dry reloads
LETHAL = 1.0         # release a partial laser charge when it already kills the target
THREAT_BOOST = 0.0  # boost while a foe is engaged or we were hit < 2.5 s ago, if energy above this (0: off)
PRACTICE_LOOT = 1.0  # practice (no FFA seen): loot/crates/NPC farming all match, laser pickups x3 while we have none
SEED_SALT = 55        # varies the bot's own random choices (strafe timing etc.); same play strength
NOOK = 0.0          # 1: hold at the least-exposed spot near the hold point
NOOK_R = 35.0    # 1: shoot back at a hot mech while breaking line of sight


class Bot:
    name = "stormrush"

    def __init__(self, hello):
        t0 = time.perf_counter()
        self.cfg = hello["config"]
        self.slot = hello.get("slot", 0)
        random.seed(hello.get("seed", 0) * 1000003 + SEED_SALT)
        d = hello.get("map", {}).get("dir", "/arena")
        self.map = None
        try:
            self.map = Map(d)
        except Exception as e:  # play blind rather than not at all
            mf.log("map load failed:", repr(e))
        gc.collect()
        gc.freeze()
        mf.log(f"init {1000 * (time.perf_counter() - t0):.0f} ms")
        self.tracks = {}          # id -> dict(pos, vel, t, hp, max_hp, weapons, cubes)
        self.attackers = {}       # id -> last time it damaged us
        self.last_dmg_t = -99.0
        self.iso_t = -99.0
        self.fspot, self.fspot_t, self.fspot_tid = None, -99.0, None
        self.last_dmg_bearing = None
        self.dmg_window = []      # (t, amount)
        self.side = 1
        self.side_until = 0.0
        self.steer_side = 1
        self.steer_side_until = 0.0
        self.hist = []            # (t, x, y)
        self.jet_until = 0.0
        self.mode = "start"
        self.retreating = False
        self.cover = None
        self.cover_t = -99.0
        self.scan_yaw = None
        self.last_aim = [0.0, 0.0]
        self.look = None          # (yaw, until)
        self.prev_hp = None
        self.dead_ids = set()
        self.take_fail = {}       # pickup id -> time
        self.crate_hits = {}
        self.slow = 0
        self.roam = None
        self.roam_t = -99.0
        self.mrm_dest = None
        self.mrm_t = -99.0
        self.my_kills = 0
        self.ffa = False          # practice squads never kill each other; a death we didn't cause => FFA
        self.nook = None          # chosen (x, y)
        self.nook_t = -99.0
        self.nook_cands = []      # pending [(x, y)]
        self.nook_scored = []     # [(score, x, y)]

    # ------------------------------------------------------------ world model
    def zone_at(self, z, t):
        if t <= z["shrink_start"]:
            return z["center"][0], z["center"][1], z["radius"]
        if t >= z["shrink_end"]:
            return z["next_center"][0], z["next_center"][1], z["next_radius"]
        f = (t - z["shrink_start"]) / max(1e-6, z["shrink_end"] - z["shrink_start"])
        c, n = z["center"], z["next_center"]
        return c[0] + (n[0] - c[0]) * f, c[1] + (n[1] - c[1]) * f, z["radius"] + (z["next_radius"] - z["radius"]) * f

    def safe_circle(self, obs):
        """Circle we want to be inside: the zone ~horizon seconds from now, shrunk by a margin."""
        z, t = obs["zone"], obs["t"]
        me = obs["self"]
        x, y = me["pos"][0], me["pos"][1]
        cx, cy, r = self.zone_at(z, t)
        d_now = hyp(x - cx, y - cy)
        # how long would it take to get into the next circle?
        nx_, ny_, nr = z["next_center"][0], z["next_center"][1], z["next_radius"]
        need = max(0.0, hyp(x - nx_, y - ny_) - nr * 0.75)
        travel = need / max(1.0, me["stats"]["walk_speed"] * 0.75)
        horizon = max(12.0, travel + 12.0)
        fx, fy, fr = self.zone_at(z, t + horizon)
        margin = min(25.0, fr * 0.2)
        return (cx, cy, r, d_now), (fx, fy, max(2.0, fr - margin))

    def update_tracks(self, obs):
        t = obs["t"]
        for m in obs["mechs"]:
            if m["kind"] != "mech":
                continue
            tr = self.tracks.get(m["id"])
            if tr is None:
                tr = self.tracks[m["id"]] = {}
            tr.update(pos=m["pos"], vel=m["vel"], t=t, hp=m.get("hp", 1400), max_hp=m.get("max_hp", 1400),
                      weapons=m.get("weapons", []), cubes=m.get("cubes", 0), charging=m.get("charging", False),
                      aim=m.get("aim", [0, 0]))
        for ev in obs["events"]:
            ty = ev.get("type")
            if ty == "damaged":
                self.last_dmg_t = t
                self.dmg_window.append((t, ev.get("amount", 0)))
                if ev.get("bearing") is not None:
                    self.last_dmg_bearing = (ev["bearing"], t, ev.get("by"))
                if ev.get("by") is not None:
                    self.attackers[ev["by"]] = t
            elif ty == "kill":
                self.my_kills += 1
                self.dead_ids.add(ev.get("target"))
                self.tracks.pop(ev.get("target"), None)
            elif ty == "take_failed":
                self.take_fail[ev.get("id")] = t
        self.dmg_window = [(a, b) for (a, b) in self.dmg_window if t - a < 3.0]
        for k in [k for k, v in self.tracks.items() if t - v["t"] > 20]:
            del self.tracks[k]

    def recent_dps(self, t):
        return sum(b for (a, b) in self.dmg_window if t - a < 3.0) / 3.0

    # ------------------------------------------------------------ movement
    def clear(self, x, y, fz, yaw):
        M = self.map
        if M is None:
            return True
        c, s = math.cos(yaw), math.sin(yaw)
        px, py = -s, c
        for dist in (3.0, 6.0, 9.0):
            bx, by = x + c * dist, y + s * dist
            if M.blocked(bx, by, fz, dist):
                return False
            if dist < 9.0:
                for lat in (-2.3, 2.3):
                    if M.blocked(bx + px * lat, by + py * lat, fz, dist):
                        return False
        return True

    def steer(self, me, yaw, t):
        x, y, fz = me["pos"]
        if self.clear(x, y, fz, yaw):
            return yaw, True
        if t > self.steer_side_until:
            self.steer_side = random.choice((1, -1))
        for off in (0.35, 0.7, 1.05, 1.4, 1.75, 2.1, 2.5, 2.9):
            for sgn in (self.steer_side, -self.steer_side):
                a = yaw + sgn * off
                if self.clear(x, y, fz, a):
                    self.steer_side = sgn
                    self.steer_side_until = t + 1.5
                    return a, True
        return yaw, False

    def path_clear(self, x, y, fz, tx, ty):
        M = self.map
        if M is None:
            return True
        d = hyp(tx - x, ty - y)
        n = max(1, int(d / 3.0))
        for k in range(1, n + 1):
            f = k / n
            if M.blocked(x + (tx - x) * f, y + (ty - y) * f, fz, 3.0):
                return False
        return True

    # ------------------------------------------------------------ aiming
    def solve_aim(self, me, tp, tv, speed, target_h, gun=True):
        mz = me["pos"][2] + me["stats"].get("muzzle_height", 7.0)
        mx, my = me["pos"][0], me["pos"][1]
        tx, ty, tz = tp[0], tp[1], tp[2] + target_h
        vx, vy, vz = tv[0], tv[1], tv[2] if len(tv) > 2 else 0.0
        lag = 0.05
        g = self.cfg["mech"]["gravity"]
        airborne = abs(vz) > 0.3
        tt = 0.0
        if gun and speed > 0:
            for _ in range(4):
                px, py = tx + vx * (tt + lag), ty + vy * (tt + lag)
                d = math.sqrt((px - mx) ** 2 + (py - my) ** 2 + (tz - mz) ** 2)
                tt = max(0.0, d / speed - 0.012)  # shot spawns 3 m out
        T = tt + lag
        px, py = tx + vx * T, ty + vy * T
        pz = tz
        if airborne:
            pz = tz + vz * T - 0.5 * g * T * T
            if self.map is not None:
                pz = max(pz, self.map.surface(px, py, tp[2] + 1) + target_h)
        dx, dy, dz = px - mx, py - my, pz - mz
        hd = hyp(dx, dy)
        yaw = math.atan2(dy, dx)
        pitch = math.atan2(dz, max(0.1, hd))
        return yaw, pitch, math.sqrt(hd * hd + dz * dz), (px, py, pz)

    def aim_err(self, me, yaw, pitch):
        ay, ap = me["aim"]
        return math.sqrt((wrap(ay - yaw) * math.cos(pitch)) ** 2 + (ap - pitch) ** 2)

    # ------------------------------------------------------------ main
    def act(self, obs):
        t_start = time.perf_counter()
        me = obs["self"]
        t = obs["t"]
        x, y, z = me["pos"]
        act = {"tick": obs["tick"]}
        self.update_tracks(obs)
        if not self.ffa and self.cfg["match"].get("mechs", 16) - obs.get("alive", 16) > self.my_kills:
            self.ffa = True
        stats = me["stats"]
        maxhp = me["max_hp"]
        hp = me["hp"]
        hpf = hp / max(1.0, maxhp)
        energy = me["energy"]

        # ---- upgrade offer
        off = obs.get("offer")
        if off and off.get("choices"):
            ids = [c["id"] for c in off["choices"]]
            best, bi = 999, 0
            for i, cid in enumerate(ids):
                pri = UPGRADE_PRIORITY_PRACTICE if (OFFENSE_IN_PRACTICE and not self.ffa) else UPGRADE_PRIORITY
                r = pri.index(cid) if cid in pri else 500
                if r < best:
                    best, bi = r, i
            act["pick"] = bi

        weapons = me["weapons"]
        gun = laser = light = mortar = None
        gun_i = laser_i = light_i = mortar_i = None
        for i, w in enumerate(weapons):
            if not w:
                continue
            k = w["kind"]
            if k == "gun":
                gun, gun_i = w, i
            elif k == "laser":
                laser, laser_i = w, i
            elif k == "lightning":
                light, light_i = w, i
            elif k == "mortar":
                mortar, mortar_i = w, i

        # ---- stuck detection
        self.hist.append((t, x, y))
        while self.hist and t - self.hist[0][0] > 1.2:
            self.hist.pop(0)

        # ---- zone
        (cx, cy, cr, d_now), (fx, fy, fr) = self.safe_circle(obs)
        outside_now = d_now > cr - 4
        d_future = hyp(x - fx, y - fy)
        zone_push = d_future > fr

        # ---- enemies
        eye = z + stats.get("eye_height", 8.0)
        mechs = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        M = self.map
        enemies = []
        for m in mechs:
            p = m["pos"]
            d = hyp(p[0] - x, p[1] - y)
            sz = 1 + 0.05 * m.get("cubes", 0)
            vis = True
            if M is not None and d > 2:
                vis = M.los(x, y, eye, p[0], p[1], p[2] + 4.5 * sz) or M.los(x, y, eye, p[0], p[1], p[2] + 8.5 * sz)
            hot = (t - self.attackers.get(m["id"], -99)) < 4.0
            enemies.append({"m": m, "d": d, "vis": vis, "hot": hot, "sz": sz})
        n_close = sum(1 for e in enemies if e["d"] < 160)
        hiding = (bool(HIDE) or (bool(FLEE_FFA) and self.ffa)) and t < HIDE_UNTIL_T and obs.get("alive", 16) > HIDE_UNTIL_ALIVE
        alive = obs.get("alive", 16)
        late = alive <= 5

        # ---- FFA discipline: only fight mechs that are a threat to us, or easy kills
        def aimed_at_me(m):
            p = m["pos"]
            b = math.atan2(y - p[1], x - p[0])
            return abs(wrap(m.get("aim", [9, 0])[0] - b)) < 0.14

        for e in enemies:
            m = e["m"]
            d = e["d"]
            close = ENGAGE_CLOSE_LATE if late else ENGAGE_CLOSE
            hot = e["hot"]
            if hot and d > FAR_HOT and not (hp > 1.3 * m.get("hp", 1400) and hpf > 0.6):
                hot = False
                e["far_hot"] = True
            threat = hot or (e["vis"] and d < close) or (e["vis"] and d < AIMED_RANGE and aimed_at_me(m) and (m.get("charging") or d < 110))
            if hiding:
                # only fight what is already on top of us
                threat = (hot and d < 70) or (e["vis"] and d < 35)
            weak = m.get("hp", 1400) < WEAK_FRAC * m.get("max_hp", 1400) and hpf > 0.6 and d < 150 and e["vis"] and not hiding
            if weak:
                p = m["pos"]
                others = sum(1 for o in enemies if o is not e and hyp(o["m"]["pos"][0] - p[0], o["m"]["pos"][1] - p[1]) < 90)
                weak = others == 0 and not late
            if PRACTICE_ENGAGE and not self.ffa and e["vis"] and d < AIMED_RANGE:
                threat = True
            if HEAL_FIRST and not self.ffa and hpf < HEAL_HP and t - self.last_dmg_t > 1.0 and d > 35:
                threat = False   # heal up out of sight first; fight it when it comes close
            if PICK_FIGHTS and not self.ffa:
                p0 = m["pos"]
                others = 0
                for tid, tr in self.tracks.items():
                    if tid == m["id"] or t - tr["t"] > 10:
                        continue
                    if hyp(tr["pos"][0] - x, tr["pos"][1] - y) < 200 or hyp(tr["pos"][0] - p0[0], tr["pos"][1] - p0[1]) < 120:
                        others += 1
                alone = others == 0
                threat = (e["vis"] and d < AIMED_RANGE and alone and hpf > PICK_HP) \
                    or (e["hot"] and d < 60) or (e["vis"] and d < 30)
            if HUNT_LONE and self.ffa:
                p0 = m["pos"]
                others = 0
                for tid, tr in self.tracks.items():
                    if tid == m["id"] or t - tr["t"] > 10:
                        continue
                    if hyp(tr["pos"][0] - p0[0], tr["pos"][1] - p0[1]) < 120 or hyp(tr["pos"][0] - x, tr["pos"][1] - y) < 120:
                        others += 1
                e["lone"] = others == 0
                threat = (e["vis"] and d < AIMED_RANGE and e["lone"] and hpf > HUNT_HP) \
                    or (e["hot"] and d < 140) or (e["vis"] and d < 45) \
                    or (e["vis"] and d < 110 and aimed_at_me(m))
                weak = False
            e["engage"] = bool(threat or weak)

        # ---- target choice
        target = None
        if enemies:
            def score(e):
                m = e["m"]
                s = e["d"] + m.get("hp", 1400) / 12.0
                if e["vis"]:
                    s -= 60
                if e["hot"]:
                    s -= 50
                if e.get("engage"):
                    s -= 100
                if FINISH_BONUS and not self.ffa and e["vis"] and m.get("hp", 1400) < 0.35 * m.get("max_hp", 1400):
                    s -= 80
                if MATCHUP and e["vis"]:
                    mp = m["pos"]
                    to_me_ = math.atan2(y - mp[1], x - mp[0])
                    arc_ = math.radians(stats.get("front_arc_deg", 120)) / 2
                    if abs(wrap(m.get("aim", [to_me_])[0] - to_me_)) > arc_:
                        s -= MATCHUP  # its back is to us: rear damage multiplier
                    tw = [w_ if isinstance(w_, str) else (w_ or {}).get("kind") for w_ in m.get("weapons", [])]
                    if my_laser and "mortar" in tw:
                        s -= MATCHUP  # laser cook-off
                    if my_light and "laser" in tw and e["d"] < 45:
                        s -= MATCHUP  # lightning shorts its laser
                return s
            my_kinds = [w_["kind"] for w_ in weapons if w_]
            my_laser, my_light = "laser" in my_kinds, "lightning" in my_kinds
            target = min(enemies, key=score)

        # ---- fight or flight
        my_eff = hp * (1 + 0.04 * min(me["cubes"], 10))
        if target is not None:
            th = target["m"].get("hp", 1400)
            outnumbered = sum(1 for e in enemies if e["d"] < 140 and (e["vis"] or e["hot"])) >= 2
            if not self.retreating:
                if (hpf < RETREAT_HP and th > hp * 0.8) or (hpf < OUTNUM_HP and outnumbered and not (FINISH_FIRST and target["vis"] and th < 260 and hpf > 0.25)) or (hpf < 0.2 and th > 200):
                    self.retreating = True
            else:
                if hpf > 0.8 or (th < hp * 0.35 and not outnumbered) or (t - self.last_dmg_t > 12 and hpf > 0.6):
                    self.retreating = False
        else:
            if self.retreating and (hpf > 0.75 or t - self.last_dmg_t > 10):
                self.retreating = False

        # ---- decide movement goal
        move_yaw = None
        move_mag = 1.0
        boost = False
        mode = "hold"
        gx = gy = None

        if outside_now or (zone_push and (target is None or d_future > fr + 40 or obs["zone"]["dps"] >= 60)):
            mode = "zone"
            gx, gy = fx, fy
            boost = outside_now and energy > 25
        elif target is not None and self.retreating and (target["vis"] or target["hot"] or target["d"] < 80):
            mode = "retreat"
            cov = self.find_cover(obs, enemies, (fx, fy, fr), t_start)
            if cov is not None:
                gx, gy = cov
            else:
                tp = target["m"]["pos"]
                away = math.atan2(y - tp[1], x - tp[0])
                # bias away-direction toward the safe zone
                zx, zy = fx - x, fy - y
                ax_, ay_ = math.cos(away) + 0.5 * zx / max(1, hyp(zx, zy)), math.sin(away) + 0.5 * zy / max(1, hyp(zx, zy))
                move_yaw = math.atan2(ay_, ax_) + self.side * 0.5
            boost = energy > 20
        elif target is not None and not target["engage"] and (target["vis"] or target.get("far_hot") or (hiding and target["hot"])) and (target["d"] < (220.0 if (hiding or (not self.ffa and (PICK_FIGHTS or (HEAL_FIRST and hpf < HEAL_HP)))) else AVOID_RANGE) or target.get("far_hot") or (hiding and target["hot"])):
            # someone else's fight or a bot not looking at us: get out of its sight, quietly
            mode = "avoid"
            cov = self.find_cover(obs, [e for e in enemies if e["vis"]] or [target], (fx, fy, fr), t_start)
            if cov is not None:
                gx, gy = cov
            else:
                tp = target["m"]["pos"]
                move_yaw = math.atan2(y - tp[1], x - tp[0])
            move_mag = 1.0 if (target["d"] < 70 or target.get("far_hot") or target["hot"] or (not self.ffa and (PICK_FIGHTS or (HEAL_FIRST and hpf < HEAL_HP)))) else 0.42
        elif target is not None and target["engage"]:
            mode = "fight"
            m = target["m"]
            tp = m["pos"]
            d = target["d"]
            bear = math.atan2(tp[1] - y, tp[0] - x)
            their = m.get("weapons", [])
            if light is not None and "lightning" not in their and hpf > 0.45:
                pref = 28.0
            elif "lightning" in their:
                pref = max(PREF_RANGE_FFA if self.ffa else PREF_RANGE, 75.0)
            else:
                pref = PREF_RANGE_FFA if self.ffa else PREF_RANGE
            if not target["vis"]:
                # close in to get a line, but stay careful
                pref = min(pref, max(20.0, d - 30))
            radial = max(-1.0, min(1.0, (d - pref) / 25.0))
            if t > self.side_until:
                if random.random() < FLIP_P:
                    self.side = -self.side
                self.side_until = t + random.uniform(STRAFE_MIN, STRAFE_MAX)
            if ISOLATE and M is not None and target["vis"]:
                others_ = [e for e in enemies if e is not target and e["vis"] and e["d"] < 180]
                if others_ and t > self.iso_t:
                    self.iso_t = t + 0.3
                    sc_ = {}
                    for sg in (1, -1):
                        a_ = bear + sg * PI / 2
                        qx, qy = x + math.cos(a_) * ISO_STEP, y + math.sin(a_) * ISO_STEP
                        if M.blocked(qx, qy, z, ISO_STEP):
                            sc_[sg] = -9.0
                            continue
                        qz = z + 8.0
                        v_ = 0.0
                        if M.los(qx, qy, qz, tp[0], tp[1], tp[2] + 4.5 * target["sz"]):
                            v_ += 1.0
                        for e_ in others_:
                            op = e_["m"]["pos"]
                            if not M.los(qx, qy, qz, op[0], op[1], op[2] + 4.5 * e_["sz"]):
                                v_ += 1.5
                            v_ += 0.01 * (hyp(qx - op[0], qy - op[1]) - e_["d"])
                        if not self.in_safe(qx, qy, (fx, fy, fr)):
                            v_ -= 3.0
                        sc_[sg] = v_
                    if abs(sc_[1] - sc_[-1]) >= 0.5:
                        self.side = 1 if sc_[1] > sc_[-1] else -1
                        self.side_until = t + 0.5
            tan = bear + self.side * PI / 2
            vx_ = radial * math.cos(bear) + STRAFE_W * math.cos(tan)
            vy_ = radial * math.sin(bear) + STRAFE_W * math.sin(tan)
            # don't strafe out of the zone
            if d_future > fr * 0.8:
                zx, zy = fx - x, fy - y
                zl = max(1.0, hyp(zx, zy))
                vx_ += 0.8 * zx / zl
                vy_ += 0.8 * zy / zl
            move_yaw = math.atan2(vy_, vx_)
            boost = energy > FIGHT_BOOST_E and (t - self.last_dmg_t < 1.5)
            if COVER_FIGHT and M is not None:
                vis_others = [e for e in enemies if e is not target and e["vis"] and e["d"] < 190]
                fsp = self.find_fight_spot(obs, target, enemies, (fx, fy, fr), pref, t_start)
                enemy_laser = any(e["vis"] and e["m"].get("charging") and aimed_at_me(e["m"]) for e in enemies)
                th_ = target["m"].get("hp", 1400)
                finishing = target["vis"] and th_ < 260 and hpf > 0.25
                my_charging = laser is not None and (laser.get("charge", 0) or 0) > 0
                reloading_dry = (gun is not None and gun.get("reload", 0) > 0.55 and target["vis"]
                                 and (laser is None or (not my_charging and laser.get("cooldown", 0) > 0.45))
                                 and (light is None or d > 42))
                duck_now = (enemy_laser and not finishing and (not my_charging or hpf < 0.7)) or reloading_dry
                ducked = False
                if duck_now:
                    cov = fsp[2] if (fsp is not None and fsp[2] is not None and hyp(fsp[2][0] - x, fsp[2][1] - y) < 20.0) else None
                    if cov is None:
                        cov = self.find_cover(obs, [e for e in enemies if e["vis"] or e["hot"]] or [target], (fx, fy, fr), t_start)
                    if cov is not None and hyp(cov[0] - x, cov[1] - y) < 22.0:
                        mode = "duck"
                        gx, gy = cov
                        boost = energy > 22
                        ducked = True
                if not ducked and fsp is not None:
                    fx_, fy_, _, fexp = fsp
                    df = hyp(fx_ - x, fy_ - y)
                    if df > 4.0:
                        to_f = math.atan2(fy_ - y, fx_ - x)
                        if vis_others:
                            move_yaw = to_f
                            if energy > 25:
                                boost = True
                        else:
                            vx_ = 0.85 * math.cos(to_f) + 0.35 * math.cos(tan)
                            vy_ = 0.85 * math.sin(to_f) + 0.35 * math.sin(tan)
                            move_yaw = math.atan2(vy_, vx_)
                    else:
                        others_ = [e for e in enemies if e is not target and e["d"] < 190]
                        to_f = math.atan2(fy_ - y, fx_ - x) if df > 0.5 else tan
                        if others_:
                            def _safe_step(ang):
                                sx_, sy_ = x + math.cos(ang) * 4.5, y + math.sin(ang) * 4.5
                                sz_ = M.surface(sx_, sy_, z + 2)
                                return not any(M.los(o["m"]["pos"][0], o["m"]["pos"][1], o["m"]["pos"][2] + 8.0 * o["sz"], sx_, sy_, sz_ + 6.0, 2.5) for o in others_[:3])
                            if not _safe_step(tan):
                                alt = bear - self.side * PI / 2
                                if _safe_step(alt):
                                    self.side = -self.side
                                    tan = alt
                                else:
                                    tan = to_f if df > 1.2 else (bear if self.side > 0 else bear + PI)
                        vx_ = 0.4 * math.cos(to_f) + 0.85 * math.cos(tan)
                        vy_ = 0.4 * math.sin(to_f) + 0.85 * math.sin(tan)
                        move_yaw = math.atan2(vy_, vx_)
            elif RELOAD_COVER and gun is not None and gun.get("reload", 0) > 0.5 and target["vis"] \
                    and (laser is None or laser.get("cooldown", 0) > 0.5) and (light is None or d > 45):
                cov = self.find_cover(obs, [e for e in enemies if e["vis"] or e["hot"]] or [target], (fx, fy, fr), t_start)
                if cov is not None:
                    mode = "duck"
                    gx, gy = cov
                    boost = energy > 20
        else:
            mode = "loot"
            goal = None
            if DROP_RUSH:
                drop = self.pick_drop(obs, me, (fx, fy, fr), enemies, hpf)
                if drop is not None:
                    gx, gy = drop
                    mode = "drop"
            if mode == "drop":
                pass
            elif (goal := self.pick_loot(obs, me, (fx, fy, fr), enemies, hpf)) is not None:
                gx, gy = goal
            elif zone_push or d_future > fr * 0.7:
                gx, gy = fx, fy
                mode = "zone"
            else:
                drop = self.pick_drop(obs, me, (fx, fy, fr), enemies, hpf)
                if drop is not None:
                    gx, gy = drop
                    mode = "drop"
                else:
                    mode = "hold"

        if gx is not None:
            dx, dy = gx - x, gy - y
            dd = hyp(dx, dy)
            if dd < 1.2:
                move_mag = 0.0
                move_yaw = 0.0
            else:
                move_yaw = math.atan2(dy, dx)
                if mode in ("drop",) and dd < 4:
                    move_mag = 0.3
                elif mode == "loot" and dd < 6:
                    move_mag = 0.6
        if gx is not None and mode == "avoid" and move_mag < 1.0:
            pass  # keep the quiet walk speed set above
        if mode == "hold":
            # hold a quiet spot between us and the safe-circle center, ~half the radius out:
            # away from the crowded middle, but never near the edge
            dx, dy = x - fx, y - fy
            dl = hyp(dx, dy)
            ring = fr * HOLD_RING
            if dl > 1:
                hx, hy = fx + dx / dl * ring, fy + dy / dl * ring
            else:
                hx, hy = fx, fy
            if HUNT_LONE and self.ffa and M is not None:
                goal = self.hunt_goal(obs, me, (fx, fy, fr), t)
                mode = "hunt"
                if goal is not None and hyp(goal[0] - x, goal[1] - y) > 4:
                    move_yaw = math.atan2(goal[1] - y, goal[0] - x)
                    move_mag = 1.0
                else:
                    move_yaw, move_mag = 0.0, 0.0
            elif MRM and M is not None:
                ax_, ay_ = (hx, hy) if dl > ring * 0.8 else (x, y)
                dest = self.mrm(obs, me, (fx, fy, fr), ax_, ay_, t)
                if dest is not None:
                    hx, hy, mag = dest
                    if hyp(hx - x, hy - y) > 2.0:
                        move_yaw = math.atan2(hy - y, hx - x)
                        move_mag = mag
                    else:
                        move_yaw, move_mag = 0.0, 0.0
                else:
                    move_yaw, move_mag = 0.0, 0.0
            elif NOOK and M is not None:
                ax_, ay_ = (hx, hy) if dl > ring * 0.8 else (x, y)
                nk = self.update_nook(obs, me, (fx, fy, fr), ax_, ay_, t_start)
                if nk is not None:
                    hx, hy = nk
                if hyp(hx - x, hy - y) > 2.5:
                    move_yaw = math.atan2(hy - y, hx - x)
                    move_mag = 0.38
                else:
                    move_yaw, move_mag = 0.0, 0.0
            elif hyp(hx - x, hy - y) > 6 and dl > ring * 0.8:
                move_yaw = math.atan2(hy - y, hx - x)
                move_mag = 0.38  # 0.38 * 11.7 m/s < step_speed: no footsteps
            else:
                move_yaw, move_mag = 0.0, 0.0

        # charging the laser halves speed; that's accepted.
        jump = False
        if move_yaw is not None and move_mag > 0:
            sy, ok = self.steer(me, move_yaw, t)
            moved = 99.0
            if len(self.hist) > 5 and t - self.hist[0][0] > 0.9:
                moved = hyp(x - self.hist[0][1], y - self.hist[0][2])
            if (not ok or (moved < 1.2 * move_mag and not (STUCK_FIX and mode in ("fight", "duck")))) and t > self.jet_until and energy > 55:
                self.jet_until = t + 2.2
            if t < self.jet_until:
                if me["grounded"]:
                    jump = True
                boost = energy > 5
                sy = move_yaw
            act["move"] = [round(math.cos(sy) * move_mag, 3), round(math.sin(sy) * move_mag, 3)]
        else:
            act["move"] = [0.0, 0.0]
        act["boost"] = bool(boost and act["move"] != [0.0, 0.0])
        act["jump"] = jump

        # ---- mortar dodge: shells we can see landing near us
        for pr in obs["projectiles"]:
            if pr["kind"] != "mortar":
                continue
            land = self.shell_land(pr, z)
            if land and hyp(land[0] - x, land[1] - y) < 12:
                a = math.atan2(y - land[1], x - land[0])
                a2, _ = self.steer(me, a, t)
                act["move"] = [math.cos(a2), math.sin(a2)]
                act["boost"] = energy > 10
                break

        # ---- bullet / laser-charge dodge
        if DODGE and mode not in ("unstuck", "duck"):
            st_ = me["stats"]
            cz_ = z + st_.get("height", 9.0) * 0.56
            reach_ = st_.get("radius", 3.0) + 1.1
            ddx = ddy = 0.0
            laser_ = False
            for pr in obs["projectiles"]:
                if pr["kind"] == "mortar":
                    continue
                pp, pv = pr["pos"], pr["vel"]
                v2 = pv[0] * pv[0] + pv[1] * pv[1] + pv[2] * pv[2]
                if v2 < 1:
                    continue
                tc = ((x - pp[0]) * pv[0] + (y - pp[1]) * pv[1] + (cz_ - pp[2]) * pv[2]) / v2
                if tc <= 0 or tc > 0.7:
                    continue
                mxx, myy = pp[0] + pv[0] * tc - x, pp[1] + pv[1] * tc - y
                if hyp(mxx, myy) < reach_ and abs(pp[2] + pv[2] * tc - cz_) < st_.get("height", 9.0) * 0.6:
                    vh = hyp(pv[0], pv[1])
                    if vh < 1:
                        continue
                    px_, py_ = -pv[1] / vh, pv[0] / vh
                    if px_ * mxx + py_ * myy > 0:
                        px_, py_ = -px_, -py_
                    ddx += px_
                    ddy += py_
            for m_ in obs["mechs"]:
                if m_.get("kind") != "mech" or not m_.get("charging"):
                    continue
                to_me = math.atan2(y - m_["pos"][1], x - m_["pos"][0])
                if abs(wrap(m_["aim"][0] - to_me)) > 0.15:
                    continue
                ddx += math.cos(to_me + PI / 2 * self.side) * 1.5
                ddy += math.sin(to_me + PI / 2 * self.side) * 1.5
                laser_ = True
            l_ = hyp(ddx, ddy)
            if l_ > 0.01:
                mv0, mv1 = act["move"]
                mx_, my_ = mv0 * 0.4 + ddx / l_ * 0.9, mv1 * 0.4 + ddy / l_ * 0.9
                l2 = hyp(mx_, my_)
                if l2 > 1:
                    mx_, my_ = mx_ / l2, my_ / l2
                mag_ = min(1.0, hyp(mx_, my_))
                a2_, _ = self.steer(me, math.atan2(my_, mx_), t)
                act["move"] = [round(math.cos(a2_) * mag_, 3), round(math.sin(a2_) * mag_, 3)]
                if (laser_ and energy > 25) or (DODGE_BOOST and energy > DODGE_BOOST):
                    act["boost"] = True

        if THREAT_BOOST and energy > THREAT_BOOST and act["move"] != [0.0, 0.0] and mode != "hold" and ((target is not None and target["vis"] and target["engage"]) or t - self.last_dmg_t < 2.5):
            act["boost"] = True

        # ---- aim & fire
        fire = [False, False]
        aim = None
        shoot = None  # (pos, vel, height, kind)
        hold_fire = False
        if target is not None and (target["vis"] or target["d"] < 230) and (target["engage"] or mode == "retreat"):
            m = target["m"]
            shoot = (m["pos"], m["vel"], 4.5 * target["sz"], "mech", target["vis"])
        elif target is not None and target["vis"] and (target["d"] < 150 or target.get("far_hot")):
            # face it (front armor) but stay silent
            m = target["m"]
            shoot = (m["pos"], m["vel"], 4.5 * target["sz"], "mech", target["vis"])
            hold_fire = not ((AVOID_SHOOT and target.get("far_hot")) or (hiding and target["hot"]))
        elif mode in ("loot", "hold", "zone", "drop") and not (SOFT_WAIT and t - self.last_dmg_t < 3.0):
            shoot = self.pick_soft_target(obs, me, npcs, hpf, enemies)

        if shoot is not None:
            tp, tv, th, kind, vis = shoot
            gspd = gun["stats"]["speed"] if gun else 252.0
            yaw, pitch, dist, P = self.solve_aim(me, tp, tv, gspd, th, gun=True)
            aim = [yaw, pitch]
            mz = z + stats.get("muzzle_height", 7.0)
            los_ok = vis if M is None else M.los(x, y, mz, P[0], P[1], P[2], step=2.0)
            if HIGH_AIM and not los_ok and kind == "mech" and M is not None:
                y2, p2, d2, P2 = self.solve_aim(me, tp, tv, gspd, th * 1.7, gun=True)
                if M.los(x, y, mz, P2[0], P2[1], P2[2], step=2.0):
                    yaw, pitch, dist, P = y2, p2, d2, P2
                    aim = [yaw, pitch]
                    los_ok = True
            err = self.aim_err(me, yaw, pitch)
            miss = err * dist
            tol = AIM_TOL if kind == "mech" else 1.4
            if gun is not None and los_ok and dist < gun["stats"]["range"] * 0.98 and miss < tol:
                if gun.get("ammo", 1) > 0 and gun.get("reload", 0) <= 0:
                    fire[gun_i] = True
            # laser: hitscan, aim at the current point (plus decision lag)
            if laser is not None:
                ly, lp, ld, LP = self.solve_aim(me, tp, tv, 0, th, gun=False)
                if HIGH_AIM and kind == "mech" and M is not None and not M.los(x, y, mz, LP[0], LP[1], LP[2], step=2.0):
                    ly2, lp2, ld2, LP2 = self.solve_aim(me, tp, tv, 0, th * 1.7, gun=False)
                    if M.los(x, y, mz, LP2[0], LP2[1], LP2[2], step=2.0):
                        ly, lp, ld, LP = ly2, lp2, ld2, LP2
                lerr = self.aim_err(me, ly, lp) * ld
                lrange = laser["stats"].get("range", 280)
                lfull = laser["stats"].get("charge", 0.75)
                charge = laser.get("charge", 0) or 0
                l_los = M is None or M.los(x, y, mz, LP[0], LP[1], LP[2], step=2.0)
                worth = kind == "mech" or (kind == "npc" and not NO_NPC_LASER)
                lethal = False
                if charge > 0:
                    hold_max = lfull + self.cfg["weapons"]["laser"].get("hold_s", 2.0)
                    lst = laser["stats"]
                    if LETHAL and kind == "mech" and target is not None and target["m"].get("hp") is not None and charge >= lst.get("min_charge", 0.15):
                        ccfg = self.cfg.get("cubes", {})
                        dmg = lst.get("damage", 315) * (lst.get("min_frac", 0.35) + (1 - lst.get("min_frac", 0.35)) * min(1.0, charge / lfull))
                        dmg *= 1 + ccfg.get("damage_per_cube", 0) * min(me.get("cubes", 0), ccfg.get("stat_cap", 0))
                        if any((w_ == "mortar" or (isinstance(w_, dict) and w_.get("kind") == "mortar")) for w_ in target["m"].get("weapons", [])):
                            dmg *= 1 + lst.get("cookoff", 0.6)
                        lethal = dmg * 0.8 >= target["m"]["hp"]
                    if RETREAT_DUMP and mode in ("retreat", "duck") and charge >= lst.get("min_charge", 0.15):
                        fire[laser_i] = False  # dump it: get full escape speed back
                    elif (charge >= lfull * 0.97 or lethal) and l_los and lerr < 1.6:
                        fire[laser_i] = False  # release -> fire
                    elif charge > hold_max - 0.25 and l_los and lerr < 3.0:
                        fire[laser_i] = False
                    else:
                        fire[laser_i] = True
                elif laser.get("cooldown", 0) <= 0.05 and worth and l_los and ld < lrange * 0.97 and mode not in ("retreat", "duck"):
                    fire[laser_i] = True
                elif laser.get("cooldown", 0) <= 0.05 and worth and l_los and ld < lrange * 0.97 and mode == "retreat" and ld > 60 and RETREAT_LASER:
                    fire[laser_i] = True
                if kind == "mech" and laser_i is not None and gun is None:
                    aim = [ly, lp]
                elif LASER_SNAP and kind == "mech" and (charge >= lfull * SNAP_FRAC or lfull - charge <= 0.16 or lethal) and l_los:
                    aim = [ly, lp]
            if light is not None:
                lst = light["stats"]
                rng = lst.get("range", 45) * 0.95
                cone = math.radians(lst.get("cone_deg", 30)) * (0.82 if (LIGHTNING_PRACTICE and not self.ffa) else LIGHT_CONE)
                ayaw = me["aim"][0]
                byaw = math.atan2(tp[1] - y, tp[0] - x)
                hd = hyp(tp[0] - x, tp[1] - y)
                if hd < rng and abs(wrap(byaw - ayaw)) < cone and los_ok and light.get("cooldown", 0) <= 0.1:
                    reserve = 12 if mode != "retreat" else 30
                    if energy > reserve + lst.get("energy", 3):
                        fire[light_i] = True
        else:
            # nothing to shoot: look at threats we heard or felt, else where we go
            look = None
            if self.last_dmg_bearing and t - self.last_dmg_bearing[1] < 2.5:
                look = self.last_dmg_bearing[0]
            else:
                best = None
                for s in obs["sounds"]:
                    if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "step", "boost", "jet", "mortar", "explosion", "reload"):
                        pri = {"near": 0, "mid": 1, "far": 2}.get(s["dist"], 2)
                        if best is None or pri < best[0]:
                            best = (pri, s["bearing"])
                if best is not None and best[0] <= 1:
                    self.look = (best[1], t + 1.5)
                if self.look and t < self.look[1]:
                    look = self.look[0]
            if look is None:
                # recent memory of an enemy we lost
                if self.tracks:
                    tr = min(self.tracks.values(), key=lambda v: t - v["t"])
                    if t - tr["t"] < 8:
                        look = math.atan2(tr["pos"][1] - y, tr["pos"][0] - x)
            if look is None and LOOT_SCAN and me["weapons"][1] is None and t < 25 and mode != "retreat":
                if self.scan_yaw is None:
                    self.scan_yaw = me["aim"][0]
                self.scan_yaw = wrap(self.scan_yaw + 0.45)
                look = self.scan_yaw
            if look is None:
                if act["move"] != [0.0, 0.0] and mode != "retreat":
                    look = math.atan2(act["move"][1], act["move"][0])
                else:
                    if self.scan_yaw is None:
                        self.scan_yaw = me["aim"][0]
                    self.scan_yaw = wrap(self.scan_yaw + 0.25)
                    look = self.scan_yaw
            aim = [look, 0.0]
            # keep the laser from auto-firing into nothing for too long: release when charged
            if laser is not None and (laser.get("charge", 0) or 0) > 0:
                fire[laser_i] = True if (laser.get("charge", 0) or 0) < laser["stats"].get("charge", 0.75) + 1.5 else False

        pmin, pmax = self.cfg["mech"].get("min_pitch", -0.7), self.cfg["mech"].get("max_pitch", 1.3)
        aim[1] = max(pmin, min(pmax, aim[1]))
        act["aim"] = [round(aim[0], 4), round(aim[1], 4)]
        if RETREAT_HOLD and mode == "retreat":
            hold_fire = True
        if hold_fire:
            fire = [False, False]
            if laser is not None and (laser.get("charge", 0) or 0) > 0:
                fire[laser_i] = True  # releasing would fire it; keep holding
        act["fire"] = fire

        # ---- reloads
        if gun is not None and gun.get("reload", 0) <= 0:
            ammo = gun.get("ammo", 0)
            mag = gun["stats"].get("mag", 12)
            if ammo == 0 or (ammo < mag * 0.5 and not fire[gun_i] and (target is None or not target["vis"])) or (TOPOFF and ammo < mag and not fire[gun_i] and target is None):
                act["reload"] = True

        # ---- pickups
        self.handle_take(obs, me, act, laser, light, mortar)

        self.mode = mode
        el = (time.perf_counter() - t_start) * 1000
        if el > 25:
            self.slow += 1
            if self.slow < 20:
                mf.log(f"slow tick {obs['tick']}: {el:.1f} ms mode={mode}")
        act["debug"] = {"label": f"{mode} hp{hp:.0f} {'R' if self.retreating else ''}"}
        return act

    # ------------------------------------------------------------ pieces
    def shell_land(self, pr, ref_z):
        g = self.cfg["weapons"]["mortar"].get("gravity", 22.0)
        px, py, pz = pr["pos"]
        vx, vy, vz = pr["vel"]
        # solve pz + vz t - g t^2/2 = ref_z
        a, b, c = -0.5 * g, vz, pz - ref_z
        disc = b * b - 4 * a * c
        if disc < 0:
            return None
        tt = (-b - math.sqrt(disc)) / (2 * a)
        if tt < 0 or tt > 6:
            return None
        return px + vx * tt, py + vy * tt

    def hunt_goal(self, obs, me, safe, t):
        """Where to go while looking for a lone mech: last seen lone enemy, then a heard shot, then roam the safe circle."""
        x, y, z = me["pos"]
        fx, fy, fr = safe
        best = None
        for tid, tr in self.tracks.items():
            age = t - tr["t"]
            if age > 20:
                continue
            p = tr["pos"]
            if hyp(p[0] - fx, p[1] - fy) > fr * 1.05:
                continue
            near = sum(1 for oid, o in self.tracks.items() if oid != tid and t - o["t"] < 10
                       and hyp(o["pos"][0] - p[0], o["pos"][1] - p[1]) < 120)
            if near:
                continue
            d = hyp(p[0] - x, p[1] - y)
            sc = d + age * 8
            if best is None or sc < best[0]:
                best = (sc, p[0], p[1])
        if best is not None:
            return (best[1], best[2])
        for s_ in obs["sounds"]:
            if s_["kind"] in ("gun", "laser", "lightning", "step", "boost") and s_["dist"] in ("near", "mid"):
                r = 50.0 if s_["dist"] == "near" else 110.0
                gx, gy = x + math.cos(s_["bearing"]) * r, y + math.sin(s_["bearing"]) * r
                if hyp(gx - fx, gy - fy) < fr:
                    self.roam, self.roam_t = (gx, gy), t
                    return self.roam
        if self.roam is None or t - self.roam_t > 15 or hyp(self.roam[0] - x, self.roam[1] - y) < 8 \
                or hyp(self.roam[0] - fx, self.roam[1] - fy) > fr:
            for _ in range(20):
                a = random.uniform(0, TAU)
                r = fr * math.sqrt(random.random()) * 0.8
                gx, gy = fx + math.cos(a) * r, fy + math.sin(a) * r
                if self.map is None or not self.map.blocked(gx, gy, self.map.gz(gx, gy), 1.0):
                    break
            self.roam, self.roam_t = (gx, gy), t
        return self.roam

    def mrm(self, obs, me, safe, ax, ay, t):
        """Minimum Risk Movement: every MRM_PERIOD s, score 12 nearby points and head for the safest."""
        M = self.map
        x, y, z = me["pos"]
        fx, fy, fr = safe
        threats = []
        for tid, tr in self.tracks.items():
            if t - tr["t"] > 15:
                continue
            p = tr["pos"]
            d = hyp(p[0] - x, p[1] - y)
            if d < 260:
                threats.append((d, p, tr.get("hp", 1400)))
        threats.sort(key=lambda q: q[0])
        threats = threats[:4]
        mag = 1.0 if (threats and threats[0][0] < 200) else 0.38
        if self.mrm_dest is not None and t - self.mrm_t < MRM_PERIOD and hyp(self.mrm_dest[0] - x, self.mrm_dest[1] - y) > 3:
            return (self.mrm_dest[0], self.mrm_dest[1], mag)
        best, bs = None, 1e18
        for k in range(12):
            a = k * TAU / 12 + random.uniform(-0.2, 0.2)
            r = random.uniform(12.0, 28.0)
            px, py = x + math.cos(a) * r, y + math.sin(a) * r
            if M.blocked(px, py, z, r):
                continue
            if hyp(px - fx, py - fy) > fr * 0.9:
                continue
            pz = M.surface(px, py, z + 2)
            risk = 0.03 * hyp(px - ax, py - ay)
            for (d0, p, ehp) in threats:
                dd = max(10.0, hyp(px - p[0], py - p[1]))
                w = (ehp / 1400.0) * 4000.0 / (dd * dd)
                if M.los(p[0], p[1], p[2] + 8.0, px, py, pz + 4.5, 2.5):
                    w *= 3.0
                risk += w
            risk += random.uniform(0, 0.05)
            if risk < bs:
                best, bs = (px, py), risk
        if best is None:
            return None
        self.mrm_dest = best
        self.mrm_t = t
        return (best[0], best[1], mag)

    def exposure(self, px, py):
        """How many of 16 ring positions (70 m and 130 m out) can see a mech standing here."""
        M = self.map
        pz = M.gz(px, py)
        n = 0
        for r in (70.0, 130.0):
            for k in range(8):
                a = k * PI / 4 + (0.39 if r > 100 else 0.0)
                ox, oy = px + math.cos(a) * r, py + math.sin(a) * r
                if M.los(ox, oy, M.gz(ox, oy) + 8.0, px, py, pz + 4.5, 2.5):
                    n += 1
        return n

    def update_nook(self, obs, me, safe, ax, ay, t_start):
        """Incrementally score candidate spots around (ax, ay); keep the least exposed."""
        M = self.map
        t = obs["t"]
        x, y, z = me["pos"]
        fx, fy, fr = safe
        if self.nook is not None:
            nx_, ny_ = self.nook
            if hyp(nx_ - fx, ny_ - fy) > fr * 0.85:
                self.nook = None
        if self.nook is not None and t - self.nook_t < 15 and not self.nook_cands:
            return self.nook
        if not self.nook_cands and not self.nook_scored:
            cands = [(ax, ay)]
            for r in (12.0, 24.0, NOOK_R):
                for k in range(6):
                    a = k * TAU / 6 + r * 0.05
                    cands.append((ax + math.cos(a) * r, ay + math.sin(a) * r))
            self.nook_cands = [c for c in cands
                               if hyp(c[0] - fx, c[1] - fy) < fr * 0.85 and not M.blocked(c[0], c[1], M.gz(c[0], c[1]), 1.0)
                               and M.top[M.idx(c[0], c[1])] < M.gz(c[0], c[1]) + 0.5]
        while self.nook_cands and time.perf_counter() - t_start < 0.010:
            cx_, cy_ = self.nook_cands.pop()
            e = self.exposure(cx_, cy_)
            self.nook_scored.append((e + hyp(cx_ - x, cy_ - y) / 40.0, cx_, cy_))
        if not self.nook_cands and self.nook_scored:
            sc, bx, by = min(self.nook_scored)
            self.nook = (bx, by)
            self.nook_t = t
            self.nook_scored = []
        return self.nook

    def in_safe(self, px, py, safe):
        fx, fy, fr = safe
        return hyp(px - fx, py - fy) < fr

    def wval(self, kind):
        if LIGHTNING_PRACTICE and not self.ffa:
            return {"lightning": 3, "laser": 2}.get(kind, WEAPON_VALUE.get(kind, 0))
        return WEAPON_VALUE.get(kind, 0)

    def wanted_pickup(self, kind, me):
        ws = me["weapons"]
        have = [w["kind"] for w in ws if w]
        if kind in have:
            return 0
        v = self.wval(kind)
        if v <= 0:
            return 0
        if ws[1] is None or ws[0] is None:
            return v
        # both full: swap the weaker non-gun weapon if this one is better
        others = [self.wval(w["kind"]) for w in ws if w and w["kind"] != "gun"]
        if others and v > min(others):
            return v - min(others)
        return 0

    def pick_loot(self, obs, me, safe, enemies, hpf):
        x, y, z = me["pos"]
        t = obs["t"]
        best, bs = None, 0.0
        threat_near = any(e["d"] < 90 for e in enemies)
        # late in the match, walking around for loot is noise and exposure: only grab what's close
        loot_r = 1e9 if t < 200 and obs.get("alive", 16) > 6 else 45.0
        if HIDE and t < HIDE_UNTIL_T and obs.get("alive", 16) > HIDE_UNTIL_ALIVE:
            loot_r = 120.0 if t < 45 else 30.0
        if PRACTICE_LOOT and not self.ffa:
            loot_r = 1e9
        for p in obs["pickups"]:
            v = self.wanted_pickup(p["kind"], me)
            if v <= 0 or t - self.take_fail.get(p["id"], -99) < 3:
                continue
            px, py = p["pos"][0], p["pos"][1]
            if abs(p["pos"][2] - z) > 25 or not self.in_safe(px, py, safe):
                continue
            d = hyp(px - x, py - y)
            if d > loot_r:
                continue
            s = (60.0 * v) / (d + 15)
            if (WEAPON_HUNT or (LIGHTNING_PRACTICE and not self.ffa)) and (me["weapons"][1] is None or (p["kind"] == "lightning" and not any(w and w["kind"] == "lightning" for w in me["weapons"]))):
                s *= 4.0
            if PRACTICE_LOOT and not self.ffa and p["kind"] == "laser" and not any(w and w["kind"] == "laser" for w in me["weapons"]):
                s *= 3.0
            if s > bs:
                best, bs = (px, py), s
        for c in obs["cubes"]:
            px, py = c["pos"][0], c["pos"][1]
            if abs(c["pos"][2] - z) > 25 or not self.in_safe(px, py, safe):
                continue
            d = hyp(px - x, py - y)
            if d > loot_r:
                continue
            s = 50.0 / (d + 15)
            if s > bs:
                best, bs = (px, py), s
        if threat_near:
            return best
        # crates: walk within ~25 m so we can shoot them, then collect
        for c in obs["crates"]:
            px, py, pz = c["pos"]
            if abs(pz - z) > 15 or not self.in_safe(px, py, safe):
                continue
            d = hyp(px - x, py - y)
            if d > loot_r:
                continue
            if d < 22:
                continue
            s = 20.0 / (d + 15)
            if s > bs:
                best, bs = (px, py), s
        return best

    def pick_drop(self, obs, me, safe, enemies, hpf):
        x, y, _ = me["pos"]
        for dr in obs.get("drops", []):
            px, py = dr["pos"][0], dr["pos"][1]
            d = hyp(px - x, py - y)
            eta = d / max(1.0, me["stats"]["walk_speed"])
            if d > (350 if DROP_RUSH else 220) or not self.in_safe(px, py, safe):
                continue
            if dr["lands_in"] > eta + 6:
                continue
            if any(hyp(e["m"]["pos"][0] - px, e["m"]["pos"][1] - py) < (45 if DROP_RUSH else 70) and (e["vis"] or not DROP_RUSH) for e in enemies):
                continue
            if dr.get("holder") not in (None, me["id"]):
                continue
            if obs.get("alive", 16) <= 5 and any(e["vis"] for e in enemies):
                continue
            return (px, py)
        return None

    def pick_soft_target(self, obs, me, npcs, hpf, enemies):
        x, y, z = me["pos"]
        M = self.map
        eye = z + me["stats"].get("eye_height", 8.0)
        best = None
        late = obs["t"] > 200 or obs.get("alive", 16) <= 6 or (HIDE and 45 < obs["t"] < HIDE_UNTIL_T)
        if PRACTICE_LOOT and not self.ffa:
            late = False
        for n in npcs:
            p = n["pos"]
            d = hyp(p[0] - x, p[1] - y)
            if d > (40 if late else 110):
                continue
            if hpf < 0.45 and d > 30:
                continue
            if M is not None and not M.los(x, y, eye, p[0], p[1], p[2] + 1.75):
                continue
            s = d + n.get("hp", 350) / 10
            if best is None or s < best[0]:
                best = (s, (p, n.get("vel", [0, 0, 0]), 1.75, "npc", True))
        if best is None:
            for c in obs["crates"]:
                p = c["pos"]
                d = hyp(p[0] - x, p[1] - y)
                if d > (30 if late else 60):
                    continue
                if M is not None and not M.los(x, y, eye, p[0], p[1], p[2] + 1.2):
                    continue
                s = d
                if best is None or s < best[0]:
                    best = (s, (p, [0, 0, 0], 1.2, "crate", True))
        return best[1] if best else None

    def handle_take(self, obs, me, act, laser, light, mortar):
        x, y, z = me["pos"]
        reach = self.cfg["loot"].get("pickup_radius", 4.0) - 0.3
        best = None
        for p in obs["pickups"]:
            px, py, pz = p["pos"]
            if hyp(px - x, py - y) > reach or abs(pz - z) > 3.8:
                continue
            v = self.wanted_pickup(p["kind"], me)
            if v > 0 and (best is None or v > best[0]):
                best = (v, p)
        if best is None:
            return
        p = best[1]
        ws = me["weapons"]
        if ws[1] is None:
            act["take"] = p["id"]
        elif ws[0] is None:
            act["take"] = p["id"]
        else:
            # replace the weaker non-gun weapon; never swap out a charging laser
            worst_i, worst_v = None, 99
            for i, w in enumerate(ws):
                if w and w["kind"] != "gun":
                    v = self.wval(w["kind"])
                    if w["kind"] == "laser" and (w.get("charge", 0) or 0) > 0:
                        continue
                    if v < worst_v:
                        worst_i, worst_v = i, v
            if worst_i is not None:
                act["take"] = p["id"]
                act["drop"] = worst_i
                act["fire"][worst_i] = False

    def near_wall(self, px, py, pz):
        M = self.map
        if M is None:
            return False
        for k in range(8):
            a = k * (PI / 4)
            idx = M.idx(px + math.cos(a) * 7.5, py + math.sin(a) * 7.5)
            if M.top[idx] > pz + 5.0 and M.base[idx] < pz + 9.0:
                return True
            if M.ground[idx] > pz + 4.5:
                return True
        return False

    def find_fight_spot(self, obs, target, enemies, safe, pref, t_start):
        M = self.map
        if M is None:
            return None
        me = obs["self"]
        t = obs["t"]
        x, y, z = me["pos"]
        mz_off = me["stats"].get("muzzle_height", 7.0)
        tm = target["m"]
        tx, ty, tz = tm["pos"]
        tsz = target["sz"]
        tid = tm.get("id")
        bear = math.atan2(ty - y, tx - x)
        others = []
        seen_ids = {tid}
        for e in enemies:
            oid = e["m"].get("id")
            if oid in seen_ids or e["d"] > 200:
                continue
            seen_ids.add(oid)
            op = e["m"]["pos"]
            others.append((e["d"], op[0], op[1], op[2] + 8.0 * e["sz"]))
        if len(others) < 3:
            for oid, tr in self.tracks.items():
                if oid in seen_ids or t - tr["t"] > 8.0:
                    continue
                op = tr["pos"]
                od = hyp(op[0] - x, op[1] - y)
                if od < 170:
                    others.append((od, op[0], op[1], op[2] + 8.0))
        others.sort(key=lambda q: q[0])
        others = others[:3]

        def sees_tgt(px, py, pz):
            mz_ = pz + mz_off
            if M.los(px, py, mz_, tx, ty, tz + 4.5 * tsz, step=2.5):
                return True
            return bool(HIGH_AIM and M.los(px, py, mz_, tx, ty, tz + 7.65 * tsz, step=2.5))

        def exp_others(px, py, pz):
            n = 0
            for (_, ox, oy, oz) in others:
                if M.los(ox, oy, oz, px, py, pz + 6.0, step=2.5):
                    n += 1
            return n

        def hidden_all(px, py, pz):
            if M.los(tx, ty, tz + 8.0 * tsz, px, py, pz + 6.0, step=2.5):
                return False
            return exp_others(px, py, pz) == 0
        if self.fspot is not None and self.fspot_tid == tid and t - self.fspot_t < 0.65:
            fx_, fy_, dxy, fexp = self.fspot
            if hyp(fx_ - x, fy_ - y) < 34.0 and (self.in_safe(fx_, fy_, safe) or not self.in_safe(x, y, safe)):
                fz_ = M.surface(fx_, fy_, z + 2)
                if sees_tgt(fx_, fy_, fz_) and exp_others(fx_, fy_, fz_) <= fexp:
                    if dxy is not None:
                        dz_ = M.surface(dxy[0], dxy[1], fz_ + 2)
                        if not hidden_all(dxy[0], dxy[1], dz_):
                            dxy = None
                    if fexp == 0 or not others or dxy is not None:
                        return (fx_, fy_, dxy, fexp)
        in_safe_now = self.in_safe(x, y, safe)
        min_dt = max(18.0, pref * 0.45)
        max_dt = max(155.0, pref * 2.3)
        cur_exp = exp_others(x, y, z) if others else 0
        cands = [(x, y, 0.0)]
        for r, off in ((9.0, 0.0), (18.0, PI / 8), (28.0, 0.0)):
            for k in range(8):
                a = bear + off + k * (PI / 4)
                cands.append((x + math.cos(a) * r, y + math.sin(a) * r, r))
        scored = []
        for px, py, r in cands:
            if (time.perf_counter() - t_start) > 0.014:
                break
            if r > 0:
                if M.blocked(px, py, z, min(r, 9.0)):
                    continue
                if in_safe_now and not self.in_safe(px, py, safe):
                    continue
                pz = M.surface(px, py, z + 2)
                if abs(pz - z) > 2.5:
                    continue
                if not self.path_clear(x, y, z, px, py):
                    continue
            else:
                pz = z
            dt = hyp(px - tx, py - ty)
            if dt < min_dt or dt > max_dt:
                continue
            if not sees_tgt(px, py, pz):
                continue
            exp = cur_exp if r == 0 else exp_others(px, py, pz)
            nw = self.near_wall(px, py, pz)
            s = -exp * 16.0 + (4.0 if nw else 0.0) - abs(dt - pref) * 0.08 - r * 0.10
            if others:
                mdo = min(hyp(px - ox, py - oy) for (_, ox, oy, _) in others)
                s += min(120.0, mdo) * 0.04
            if r == 0:
                s += 0.8
            scored.append((s, px, py, pz, exp))
        if not scored:
            self.fspot = None
            return None
        scored.sort(key=lambda q: -q[0])
        best = None
        away_t = bear + PI
        for base_s, px, py, pz, exp in scored[:4]:
            duck_xy = None
            for r_cov in (9.0, 14.0):
                if duck_xy is not None or (time.perf_counter() - t_start) >= 0.018:
                    break
                for k in range(8):
                    a = away_t + (k - 3.5) * (PI / 4)
                    cx_, cy_ = px + math.cos(a) * r_cov, py + math.sin(a) * r_cov
                    if M.blocked(cx_, cy_, pz, min(r_cov, 9.0)):
                        continue
                    if in_safe_now and not self.in_safe(cx_, cy_, safe):
                        continue
                    cz_ = M.surface(cx_, cy_, pz + 2)
                    if abs(cz_ - pz) > 2.5:
                        continue
                    if not self.path_clear(px, py, pz, cx_, cy_):
                        continue
                    if hidden_all(cx_, cy_, cz_):
                        duck_xy = (cx_, cy_)
                        break
            tot = base_s + (5.0 if (duck_xy is not None and exp == 0) else (3.5 if duck_xy is not None else 0.0))
            if best is None or tot > best[0]:
                best = (tot, px, py, duck_xy, exp)
        if best is None:
            self.fspot = None
            return None
        _, bx, by, bduck, bexp = best
        if not ((others and bexp < cur_exp) or (others and bexp == 0) or bduck is not None):
            self.fspot = None
            return None
        res = (bx, by, bduck, bexp)
        self.fspot = res
        self.fspot_t = t
        self.fspot_tid = tid
        return res

    def find_cover(self, obs, enemies, safe, t_start):
        """A nearby reachable point no visible threat can see. Cached ~1 s."""
        M = self.map
        if M is None:
            return None
        me = obs["self"]
        t = obs["t"]
        x, y, z = me["pos"]
        if not enemies:
            return None
        threats = [e for e in enemies if e["d"] < 220 and (e["vis"] or e["hot"])]
        if not threats:
            threats = [min(enemies, key=lambda e: e["d"])]
        threats = sorted(threats, key=lambda e: e["d"])[:int(COVER_THREATS)]

        def hidden(px, py, pz):
            for e in threats:
                q = e["m"]["pos"]
                if M.los(q[0], q[1], q[2] + 8.0 * e["sz"], px, py, pz + 6.0, step=2.0):
                    return False
            return True

        if self.cover is not None and t - self.cover_t < 1.0:
            cx_, cy_ = self.cover
            if hyp(cx_ - x, cy_ - y) > 2.0:
                return self.cover
        if self.cover is not None and t - self.cover_t < 3.0:
            cx_, cy_ = self.cover
            if hidden(cx_, cy_, M.surface(cx_, cy_, z + 2)):
                if hyp(cx_ - x, cy_ - y) > 2.0:
                    return self.cover
                return (x, y) if hidden(x, y, z) else self.cover
        tq = threats[0]["m"]["pos"]
        away = math.atan2(y - tq[1], x - tq[0])
        best, bs = None, -1e9
        for r in (12.0, 24.0, 40.0):
            for k in range(12):
                if (time.perf_counter() - t_start) > 0.018:
                    break
                a = away + (k - 5.5) * (TAU / 12)
                px, py = x + math.cos(a) * r, y + math.sin(a) * r
                if M.blocked(px, py, z, r):
                    continue
                if not self.in_safe(px, py, safe) and self.in_safe(x, y, safe):
                    continue
                if not self.path_clear(x, y, z, px, py):
                    continue
                pz = M.surface(px, py, z + 2)
                if not hidden(px, py, pz):
                    continue
                dth = min(hyp(px - e["m"]["pos"][0], py - e["m"]["pos"][1]) for e in threats)
                s = dth * 0.5 - r * 1.0 - abs(wrap(a - away)) * 8
                if s > bs:
                    best, bs = (px, py), s
        if best is not None:
            self.cover = best
            self.cover_t = t
        return best


if __name__ == "__main__":
    mf.run(Bot)
