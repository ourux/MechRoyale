import sim, random, sys, collections, statistics
from multiprocessing import Pool
def job(seed):
    pop = sys.argv[2].split(',')
    specs = []
    for item in pop:
        name, n = item.split(':'); specs += [name]*int(n)
    random.Random(seed).shuffle(specs)
    return sim.run_match((specs, seed))
if __name__ == '__main__':
    N = int(sys.argv[1])
    pts = collections.defaultdict(list); kills = collections.defaultdict(list); wins = collections.Counter()
    with Pool(2) as p:
        for res in p.imap_unordered(job, range(1000, 1000+N)):
            for spec, place, k, _ in res:
                pts[spec].append(16-place); kills[spec].append(k)
                if place == 1: wins[spec]+=1
    for s in sorted(pts):
        v = pts[s]; se = statistics.stdev(v)/len(v)**0.5
        print(f"{s:14s} pts {statistics.mean(v):5.2f} ± {se:.2f}  kills {statistics.mean(kills[s]):.2f}  wins {wins[s]}  n={len(v)}")
