# Local free-for-all simulator

A rough re-implementation of the mechfight rules for A/B testing bots offline: 16 bots in one
process, real map, guns / lasers / lightning, vision with memory, sound, zone phases, cubes and
upgrades. Not modelled: NPC drones, crates, supply drops, mortar fire, mech growth per cube,
mech-mech pushing. Treat results as directional, then confirm on the real board.

Needs the kit's `arena/` folder; by default it looks in `../../mechfight-kit/arena`, or set
`MECH_ARENA=/path/to/arena`.

```sh
cd sim
python3 sim.py                                   # one match, prints placements
python3 variants.py "r7=HOLD_RING:0.7"           # writes bot_r7.py from ../bot.py with that constant
python3 ab.py 150 bot_v1:6,bot_starter:2,bot:4,bot_r7:4   # mean points (16 - place) per bot
```

`bot` is the current `../bot.py`; `bot_v1` (first submission) is the aggressive reference
opponent; `bot_starter` is the kit's example bot.
