#!/usr/bin/env python3
"""Draft-day engine. Feed it the names already drafted (one per line on stdin,
or in drafted.txt) and it prints the recommendation for slot 7's next pick."""
import json, sys, re, unicodedata, datetime, zoneinfo

SLOT, TEAMS, ROUNDS = 7, 12, 15
MY_PICKS = [(r, (r - 1) * TEAMS + (SLOT if r % 2 else TEAMS - SLOT + 1)) for r in range(1, ROUNDS + 1)]
NEED = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "K": 1, "DEF": 1, "BN": 5}

board = json.load(open("/home/claude/rows.json"))

def key(n):
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode()
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", n.lower())
    return " ".join(re.sub(r"[^a-z ]", "", n).split())

BY_KEY = {key(p["n"]): p for p in board}

def load_drafted(path="/home/claude/drafted.txt"):
    try:
        lines = open(path).read().splitlines()
    except FileNotFoundError:
        lines = []
    out = []
    for ln in lines:
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln)
    return out

def main():
    drafted_raw = load_drafted()
    drafted = {key(n) for n in drafted_raw}
    n_made = len(drafted_raw)
    next_overall = n_made + 1

    mine = [(r, p) for r, p in MY_PICKS if p >= next_overall]
    my_next = mine[0] if mine else None

    avail = [p for p in board if key(p["n"]) not in drafted]
    avail.sort(key=lambda p: -p["v"])

    ny = zoneinfo.ZoneInfo("America/New_York")
    print(f"[{datetime.datetime.now(ny):%I:%M:%S %p}]  picks made: {n_made}   on the clock: overall #{next_overall}")
    if my_next:
        r, p = my_next
        away = p - next_overall
        flag = "  <<< YOU ARE ON THE CLOCK" if away == 0 else f"  ({away} picks away)"
        print(f"Your next: Round {r}, overall #{p}{flag}")
    else:
        print("Your draft is complete.")

    # roster so far
    # myroster.txt lines may be "Name" or "Name|POS" for players off the top-80 board
    roster, counts = [], {}
    for line in load_drafted("/home/claude/myroster.txt"):
        nm, _, pos_hint = line.partition("|")
        nm = nm.strip()
        pl = BY_KEY.get(key(nm))
        pos = pl["pos"] if pl else (pos_hint.strip().upper() or "??")
        label = pl["p"] if pl else pos
        roster.append((nm, label, pos))
        counts[pos] = counts.get(pos, 0) + 1
    if roster:
        print("Roster: " + ", ".join(f"{n} ({l})" for n, l, _ in roster))
        print("Counts: " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        starters = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}
        holes = [f"{p} x{n - counts.get(p, 0)}" for p, n in starters.items() if counts.get(p, 0) < n]
        flexable = sum(max(0, counts.get(p, 0) - starters[p]) for p in ("RB", "WR", "TE"))
        print("Unfilled starters: " + (", ".join(holes) if holes else "none")
              + f"   |   flex-eligible spares: {flexable}/2")

    print("\n--- BEST AVAILABLE BY VORP ---")
    print("{:<3} {:<24} {:<6} {:>7} {:>6} {:>6}".format("#", "PLAYER", "POS", "VORP", "ADP", "EDGE"))
    for i, pl in enumerate(avail[:14], 1):
        e = f"+{pl['e']}" if pl["e"] > 0 else str(pl["e"])
        print("{:<3} {:<24} {:<6} {:>7.1f} {:>6.1f} {:>6}".format(i, pl["n"], pl["p"], pl["v"], pl["a"], e))

    print("\n--- BEST AT EACH POSITION ---")
    for pos in ("RB", "WR", "TE", "QB"):
        top = [p for p in avail if p["pos"] == pos][:3]
        if top:
            print(f"  {pos}: " + " | ".join(f"{p['n']} ({p['v']:.0f})" for p in top))

    # positional cliff warning: gap between best available and the 3rd best at that position
    print("\n--- CLIFF WATCH (drop from best available to next-next at position) ---")
    for pos in ("RB", "WR", "TE", "QB"):
        pool = [p for p in avail if p["pos"] == pos]
        if len(pool) >= 3:
            gap = pool[0]["v"] - pool[2]["v"]
            mark = "  *** STEEP" if gap >= 25 else ""
            print(f"  {pos}: {pool[0]['v']:.0f} -> {pool[2]['v']:.0f}  (drop {gap:.0f}){mark}")

if __name__ == "__main__":
    main()
