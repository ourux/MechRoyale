import practice, collections, statistics, sim
from multiprocessing import Pool
def one(seed):
    m = practice.PracticeMatch("bot", seed)
    b = m.bots[0]; p = m.mechs[0]
    modes = collections.Counter(); info = {}
    orig = b.act
    def act(o):
        a = orig(o); modes[b.mode] += 1; return a
    b.act = act
    od = m.damage
    dmg = collections.Counter()
    def damage(tgt, src, amt, w, fx, fy):
        if tgt is p: dmg[w] += amt
        od(tgt, src, amt, w, fx, fy)
    m.damage = damage
    place, kills, t = m.run()
    active = m.active_set()
    near = sum(1 for h in m.mechs[1:] if h.alive and ((h.x-p.x)**2+(h.y-p.y)**2)**.5 < 150)
    return dict(pts=16-place, kills=kills, t=t, modes=modes, dmg=dmg, near=near, weapons=[w['kind'] for w in p.weapons if w], cubes=p.cubes, alive=p.alive)
if __name__ == '__main__':
    with Pool(2) as pool: R = pool.map(one, range(5000, 5060))
    M = collections.Counter(); D = collections.Counter(); W = collections.Counter(); near = collections.Counter()
    for r in R:
        M.update(r['modes']); D.update(r['dmg']); W[tuple(sorted(r['weapons']))] += 1; near[r['near']] += 1
    tot = sum(M.values())
    print('modes %', {k: round(100*v/tot) for k, v in M.most_common()})
    print('damage taken by weapon', {k: round(v) for k, v in D.items()})
    print('loadout at end', dict(W))
    print('hunters within 150 m at death', dict(sorted(near.items())))
    print('cubes mean', statistics.mean(r['cubes'] for r in R), 'survived', sum(r['alive'] for r in R))
    print('death time quartiles', statistics.quantiles([r['t'] for r in R], n=4))
