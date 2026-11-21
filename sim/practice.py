"""Practice-mode proxy: one candidate bot vs a 15-bot house squad that hunts it.

House squad (as the track page describes it): 4 easy, 4 medium, 4 hard, 3 expert; they share
what they see (here: always know the candidate's position), never damage each other, and hunt
weakest first: one at the start, more as the candidate thins them out, all of them late.
Tier skill (aim noise, reaction, weapons) is a guess, calibrated so the kit's starter bot and
our submitted versions land near their real practice scores.

    python3 practice.py 40 bot bot_v1 bot_starter
"""
import math
import random
import sys
import statistics
from multiprocessing import Pool

import sim

TIERS = (["easy"] * 4) + (["medium"] * 4) + (["hard"] * 4) + (["expert"] * 3)
SKILL = {  # aim sigma (deg), reaction (s), pref range, chance of a second weapon
    "easy": (3.2, 0.9, 60.0, 0.0),
    "medium": (2.0, 0.6, 60.0, 0.25),
    "hard": (1.1, 0.35, 70.0, 0.6),
    "expert": (0.6, 0.2, 70.0, 1.0),
}
LATE_T = 300.0  # everyone hunts after this


class HouseStub:
    def __init__(self, hello):
        pass

    def act(self, obs):
        return {}


sys.modules.setdefault("house_stub", type(sys)("house_stub"))
sys.modules["house_stub"].Bot = HouseStub


class PracticeMatch(sim.Match):
    def __init__(self, cand, seed, skill=None):
        specs = [cand] + ["house_stub"] * 15
        super().__init__(specs, seed)
        self.skill = skill or SKILL
        self.tier = {}
        self.seen_t = {}
        self.side = {}
        self.side_t = {}
        order = list(range(1, 16))
        for i, tr in zip(order, TIERS):
            self.tier[i] = tr
            m = self.mechs[i]
            if self.rng.random() < self.skill[tr][3]:
                k = self.rng.choice(["laser", "lightning"])
                m.weapons[1] = m.new_weapon(k)
        self.player = self.mechs[0]

    # house bots never hurt each other
    def damage(self, tgt, src, amount, weapon, fx, fy):
        if src is not None and isinstance(src, sim.Mech) and isinstance(tgt, sim.Mech) and src.id != 0 and tgt.id != 0:
            return
        super().damage(tgt, src, amount, weapon, fx, fy)

    def obs(self, m):
        if m.id != 0:
            return {"tick": self.tick}
        return super().obs(m)

    def active_set(self):
        dead = sum(1 for m in self.mechs[1:] if not m.alive)
        n = 15 if self.t >= LATE_T else 1 + dead
        alive = [m for m in self.mechs[1:] if m.alive]  # already weakest-first by id
        return set(m.id for m in alive[:n])

    def house_act(self, m, active):
        p = self.player
        tr = self.tier[m.id]
        sigma, react, pref, _ = self.skill[tr]
        a = {"tick": self.tick}
        dx, dy = p.x - m.x, p.y - m.y
        d = math.hypot(dx, dy)
        bear = math.atan2(dy, dx)
        vis = d < 200 and self.M.los(m.x, m.y, m.z + 8, p.x, p.y, p.z + 4.5, 2.0)
        if vis:
            self.seen_t.setdefault(m.id, self.t)
        else:
            self.seen_t.pop(m.id, None)
        engaged = m.id in active or (vis and d < 110)
        # zone first
        cx, cy, cr, _ = self.zone_now()
        z = self.zone_obs()
        nx_, ny_, nr = z["next_center"][0], z["next_center"][1], z["next_radius"]
        mv = (0.0, 0.0)
        if math.hypot(m.x - cx, m.y - cy) > cr - 8 or (not engaged and math.hypot(m.x - nx_, m.y - ny_) > nr * 0.8):
            g = math.atan2(ny_ - m.y, nx_ - m.x)
            mv = (math.cos(g), math.sin(g))
        elif engaged:
            if d > pref or not vis:
                g = bear
            else:
                if self.t > self.side_t.get(m.id, 0):
                    self.side[m.id] = -self.side.get(m.id, 1)
                    self.side_t[m.id] = self.t + self.rng.uniform(0.6, 1.6)
                g = bear + self.side.get(m.id, 1) * math.pi / 2
            g = self.steer(m, g)
            mv = (math.cos(g), math.sin(g))
        a["move"] = [mv[0], mv[1]]
        a["boost"] = engaged and d > pref + 40 and m.energy > 40
        # stuck: hop
        if engaged and math.hypot(m.vx, m.vy) < 1.0 and mv != (0.0, 0.0) and m.energy > 50:
            a["jump"] = True
            a["boost"] = True
        # aim and fire
        if engaged and (vis or d < 230):
            spd = m.wstats("gun")["speed"]
            tt = d / spd
            px, py = p.x + p.vx * (tt + 0.05), p.y + p.vy * (tt + 0.05)
            yaw = math.atan2(py - m.y, px - m.x) + math.radians(self.rng.gauss(0, sigma))
            pitch = math.atan2(p.z + 4.5 - (m.z + 7), max(1.0, d)) + math.radians(self.rng.gauss(0, sigma * 0.5))
            a["aim"] = [yaw, pitch]
            ready = vis and self.t - self.seen_t.get(m.id, self.t) >= react
            err = abs(sim.wrap(m.aim[0] - math.atan2(dy, dx)))
            fire = [False, False]
            if ready and err * d < 4.0 + sigma:
                fire[0] = True
                w = m.weapons[1]
                if w is not None:
                    if w["kind"] == "lightning":
                        fire[1] = d < 42
                    elif w["kind"] == "laser":
                        st = m.wstats("laser")
                        fire[1] = not (w.get("charge", 0) >= st["charge"])  # hold to full, then release
            a["fire"] = fire
        else:
            a["aim"] = [math.atan2(mv[1], mv[0]) if mv != (0.0, 0.0) else m.aim[0], 0.0]
        return a

    def steer(self, m, yaw):
        for off in (0, 0.4, -0.4, 0.8, -0.8, 1.2, -1.2, 1.7, -1.7, 2.4, -2.4):
            a = yaw + off
            ok = True
            for r in (3.0, 6.0):
                if self.solid(m.x + math.cos(a) * r, m.y + math.sin(a) * r, m.z, 9.0):
                    ok = False
                    break
            if ok:
                return a
        return yaw

    def run(self):
        dur = sim.CFG["match"]["duration_s"]
        every = sim.CFG["match"]["decide_every"]
        while True:
            if not self.player.alive or self.t >= dur or sum(1 for m in self.mechs if m.alive) <= 1:
                break
            if self.tick % every == 0:
                active = self.active_set()
                for m, b in zip(self.mechs, self.bots):
                    if not m.alive:
                        continue
                    if m.id == 0:
                        o = self.obs(m)
                        try:
                            act = b.act(o) or {}
                        except Exception as e:
                            act = {}
                            if not getattr(b, "_crashed", False):
                                b._crashed = True
                                print("bot crash", repr(e), file=sys.stderr)
                    else:
                        act = self.house_act(m, active)
                    self.apply_action(m, act)
                self.sounds = [s for s in self.sounds if self.t - s[0] <= 0.1 + 1e-6]
            for m in self.mechs:
                if m.alive:
                    self.physics(m)
            for m in self.mechs:
                if m.alive:
                    self.fire(m)
            for th in self.things:
                if th.alive and th.kind == "npc":
                    self.npc_step(th)
            self.step_bullets()
            for m in self.mechs:
                if m.alive:
                    self.timers(m)
            self.tick += 1
            self.t = self.tick * sim.DT
        alive = sum(1 for m in self.mechs if m.alive)
        if self.player.alive:
            # survivors ranked by HP
            better = sum(1 for m in self.mechs[1:] if m.alive and m.hp > self.player.hp)
            place = better + 1
        else:
            # place = mechs alive just before the player died (same-tick deaths share it)
            tk = next(t for t, i in self.deaths if i == 0)
            place = alive + sum(1 for t, i in self.deaths if t == tk)
        return place, self.player.kills, round(self.t)


def job(args):
    cand, seed = args
    return cand, PracticeMatch(cand, seed).run()


if __name__ == "__main__":
    n = int(sys.argv[1])
    cands = sys.argv[2:]
    res = {c: [] for c in cands}
    with Pool(2) as p:
        for cand, (place, kills, t) in p.imap_unordered(job, [(c, 5000 + s) for c in cands for s in range(n)]):
            res[cand].append((16 - place, kills, t))
    for c in cands:
        v = [r[0] for r in res[c]]
        se = statistics.stdev(v) / len(v) ** 0.5 if len(v) > 1 else 0
        print(f"{c:14s} practice {statistics.mean(v):5.2f} ± {se:.2f}  kills {statistics.mean(r[1] for r in res[c]):.2f}  "
              f"death t {statistics.mean(r[2] for r in res[c]):.0f}s  n={len(v)}")
