#!/usr/bin/env python3
"""Score all 12 rosters by optimal starting lineup (1QB/2RB/2WR/1TE/2FLEX)
using the same half-PPR projections that drove the draft board."""
import json, re, unicodedata

RAW = open("/home/claude/allpicks.txt").read().strip().splitlines()

def key(n):
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode()
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", n.lower())
    return " ".join(re.sub(r"[^a-z ]", "", n).split())

# projections keyed by name -> (pts, pos)
PROJ = {}
for p in json.load(open("/home/claude/board.json")):
    PROJ[key(p["name"])] = (p["pts"], p["pos"], p["posrank"])

# manual aliases for Sleeper spellings that differ from the projection source
ALIAS = {"kenny gainwell": "kenneth gainwell", "brian thomas": "brian thomas",
         "marvin harrison": "marvin harrison", "harold fannin": "harold fannin",
         "michael pittman": "michael pittman", "luther burden": "luther burden",
         "jeremiyah love": "jeremiyah love", "dj moore": "dj moore"}

SLOT_TO_ROSTER = {1:1,2:5,3:8,4:11,5:2,6:7,7:4,8:10,9:6,10:3,11:9,12:12}
ROSTER_TO_SLOT = {v: k for k, v in SLOT_TO_ROSTER.items()}
TEAM_NAME = {4:"Anthony (Tony Romo's DUI Mugshot)"}

rosters = {}
unmatched = set()
for line in RAW:
    parts = line.split("|")
    if len(parts) != 4:
        continue
    pick, rid, name, pos = int(parts[0]), int(parts[1]), parts[2].strip(), parts[3].strip()
    k = ALIAS.get(key(name), key(name))
    pts, ppos, prank = PROJ.get(k, (None, pos, None))
    if pts is None and pos in ("QB", "RB", "WR", "TE"):
        unmatched.add(name)
    rosters.setdefault(rid, []).append(
        {"pick": pick, "name": name, "pos": pos, "pts": pts, "prank": prank})

def best_lineup(players):
    """1QB 2RB 2WR 1TE 2FLEX(RB/WR/TE). Unprojected players score 0."""
    pool = {p: sorted([x for x in players if x["pos"] == p and x["pts"] is not None],
                      key=lambda x: -x["pts"]) for p in ("QB", "RB", "WR", "TE")}
    used, total, lineup = set(), 0.0, []
    def take(pos, n, label):
        nonlocal total
        c = 0
        for x in pool[pos]:
            if c >= n: break
            if id(x) in used: continue
            used.add(id(x)); total += x["pts"]; lineup.append((label, x)); c += 1
    take("QB", 1, "QB"); take("RB", 2, "RB"); take("WR", 2, "WR"); take("TE", 1, "TE")
    flex = sorted([x for p in ("RB", "WR", "TE") for x in pool[p] if id(x) not in used],
                  key=lambda x: -x["pts"])[:2]
    for x in flex:
        used.add(id(x)); total += x["pts"]; lineup.append(("FLEX", x))
    return total, lineup

scored = []
for rid, players in rosters.items():
    tot, lu = best_lineup(players)
    scored.append((tot, rid, lu, players))
scored.sort(reverse=True, key=lambda x: x[0])

print("PROJECTED STARTING LINEUP TOTALS (half-PPR, K/DEF excluded — they're noise)\n")
print("{:<5} {:<7} {:<9} {:>9}  {}".format("RANK", "SLOT", "ROSTER", "PTS", ""))
for i, (tot, rid, lu, _) in enumerate(scored, 1):
    tag = "  <<< YOU" if rid == 4 else ""
    print("{:<5} {:<7} {:<9} {:>9.1f}{}".format(i, ROSTER_TO_SLOT[rid], rid, tot, tag))

me = [s for s in scored if s[1] == 4][0]
print(f"\n\nYOUR OPTIMAL LINEUP  ({me[0]:.1f} pts)")
order = {"QB":0,"RB":1,"WR":2,"TE":3,"FLEX":4}
for label, x in sorted(me[2], key=lambda t: (order[t[0]], -t[1]["pts"])):
    print(f"  {label:<5} {x['name']:<24} {x['prank']:<6} {x['pts']:>6.1f}   (pick {x['pick']})")
bench = [p for p in me[3] if p not in [x for _, x in me[2]]]
print("\n  BENCH / K / DEF")
for p in sorted(bench, key=lambda p: p["pick"]):
    v = f"{p['pts']:.1f}" if p["pts"] is not None else "  n/a"
    print(f"        {p['name']:<24} {p['pos']:<6} {v:>6}   (pick {p['pick']})")

avg = sum(s[0] for s in scored) / len(scored)
print(f"\nLeague average lineup: {avg:.1f}   |   You: {me[0]:.1f}   |   Gap: {me[0]-avg:+.1f}")
if unmatched:
    print(f"\n(No projection for {len(unmatched)} drafted players — scored as 0 for everyone equally.)")
