# vmech — Mech Royale bot

Python, standard library only. `bot.py` is the bot; `mechfight.py` is the kit wrapper, unchanged.

Check the image the way the scorer does (from the kit folder, with this repo built as `vmech`):

```sh
docker build -t vmech .
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" vmech
```
