import sim, collections, sys
orig_damage = sim.Match.damage
def damage(self, tgt, src, amount, weapon, fx, fy):
    tgt._lastw = weapon
    self.dmg[(type(self.bots[src.id]).__module__ if src else 'zone', weapon)] += amount
    orig_damage(self, tgt, src, amount, weapon, fx, fy)
sim.Match.damage = damage
specs = ["bot_v1"]*6+["bot"]*6+["bot_starter"]*4
causes = collections.Counter(); dmg = collections.Counter(); dtimes=collections.defaultdict(list)
for seed in range(int(sys.argv[1])):
    m = sim.Match(specs, seed); m.dmg = collections.Counter()
    m.run()
    for mech, s in zip(m.mechs, specs):
        if not mech.alive:
            causes[(s, getattr(mech,'_lastw','?'))]+=1
    for tk, mid in m.deaths: dtimes[specs[mid]].append(round(tk/20))
    dmg.update(m.dmg)
    print(seed, 'end t', round(m.t), 'survivors', [specs[x.id] for x in m.mechs if x.alive])
for k,v in sorted(causes.items()): print(k,v)
for k,v in sorted(dmg.items()): print(k, round(v))
for k,v in dtimes.items(): print(k, sorted(v))
