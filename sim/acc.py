import sim, collections, random
fired = collections.Counter(); hits = collections.Counter(); lfire=collections.Counter(); lhit=collections.Counter()
of = sim.Match.fire; od = sim.Match.damage; ol = sim.Match.laser
def fire(self, m):
    n = len(self.bullets); of(self, m)
    fired[self.specs[m.id]] += len(self.bullets) - n
def laser(self, m, *a):
    lfire[self.specs[m.id]] += 1; ol(self, m, *a)
def damage(self, tgt, src, amt, w, fx, fy):
    if src is not None and w == 'gun': hits[self.specs[src.id]] += 1
    if src is not None and w == 'laser': lhit[self.specs[src.id]] += 1
    od(self, tgt, src, amt, w, fx, fy)
sim.Match.fire = fire; sim.Match.damage = damage; sim.Match.laser = laser
for seed in range(12):
    specs = ["bot_v1"]*5 + ["bot"]*6 + ["bot_starter"]*5
    random.Random(seed).shuffle(specs)
    m = sim.Match(specs, seed); m.specs = specs; m.run()
for s in fired:
    print(s, 'gun', fired[s], 'hit%', round(100*hits[s]/max(1,fired[s])), ' laser', lfire[s], 'hit%', round(100*lhit[s]/max(1,lfire[s])))
