#!/usr/bin/env python3
"""Blend VORP board with market ADP (FantasyFootballCalculator + FFToday)."""
import json, unicodedata, re

board = json.load(open("/home/claude/board.json"))

def key(n):
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode()
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", n.lower())
    n = re.sub(r"[^a-z ]", "", n)
    return " ".join(n.split())

FFC = """Jahmyr Gibbs 1.5|Bijan Robinson 2.3|Puka Nacua 3.0|Ja'Marr Chase 3.9|Jaxon Smith-Njigba 5.2|Jonathan Taylor 6.0|Christian McCaffrey 6.6|Amon-Ra St. Brown 7.9|James Cook 9.2|Derrick Henry 9.7|De'Von Achane 11.7|Drake London 12.2|CeeDee Lamb 12.5|Justin Jefferson 14.3|Ashton Jeanty 14.8|Chase Brown 15.3|Rashee Rice 15.3|Saquon Barkley 17.4|A.J. Brown 19.6|George Pickens 19.8|Nico Collins 20.9|Kenneth Walker 21.5|Chris Olave 23.8|Omarion Hampton 24.0|Josh Jacobs 24.0|Zay Flowers 25.7|Kyren Williams 27.2|Malik Nabers 28.0|Jeremiyah Love 29.4|Garrett Wilson 31.4|Tetairoa McMillan 31.5|DeVonta Smith 32.1|Josh Allen 33.0|Javonte Williams 33.4|Emeka Egbuka 34.1|Cam Skattebo 35.3|Breece Hall 35.9|Tee Higgins 36.6|Trey McBride 38.0|Davante Adams 38.0|Travis Etienne 39.4|Jameson Williams 40.2|Ladd McConkey 40.9|D'Andre Swift 43.0|Brock Bowers 43.5|Terry McLaurin 44.7|Bucky Irving 45.8|Rome Odunze 47.8|Jaylen Waddle 48.1|D.J. Moore 48.8|Drake Maye 51.5|Quinshon Judkins 51.6|Bhayshul Tuten 53.3|Mike Evans 54.6|David Montgomery 55.0|Christian Watson 56.3|Lamar Jackson 56.8|Alec Pierce 57.0|Joe Burrow 57.3|Courtland Sutton 59.1|TreVeyon Henderson 61.1|Luther Burden III 61.6|Colston Loveland 61.7|Jaylen Warren 63.3|DK Metcalf 63.6|Marvin Harrison Jr. 64.7|Parker Washington 65.3|Dak Prescott 65.7|Rhamondre Stevenson 67.1|Tony Pollard 67.1|Brian Thomas Jr. 69.5|Jayden Daniels 70.0|Tyler Warren 72.2|Carnell Tate 74.4|Jadarian Price 74.6|Rico Dowdle 75.5|Michael Wilson 75.9|Chris Godwin 77.0|Matthew Stafford 78.3"""

FFT = """Jahmyr Gibbs 1|Bijan Robinson 2|Ja'Marr Chase 3|Puka Nacua 4|Christian McCaffrey 5.5|Jaxon Smith-Njigba 6|Amon-Ra St. Brown 6.5|Jonathan Taylor 8|CeeDee Lamb 9|James Cook 10|Justin Jefferson 11.5|Saquon Barkley 12|Ashton Jeanty 13|Kenneth Walker 13.5|Chase Brown 15|De'Von Achane 17|Omarion Hampton 17|Derrick Henry 18|Drake London 19|Brock Bowers 20|A.J. Brown 21|Nico Collins 21.5|George Pickens 22.5|Malik Nabers 24|Jeremiyah Love 25.5|DeVonta Smith 26|Breece Hall 27.5|Zay Flowers 30|Trey McBride 30.5|Rashee Rice 30.5|Kyren Williams 32|Tee Higgins 32|Josh Allen 32.5|Chris Olave 32.5|Emeka Egbuka 35|Jaylen Waddle 35.5|Javonte Williams 36|Ladd McConkey 37.5|Josh Jacobs 39|Tetairoa McMillan 39.5|Garrett Wilson 41|Colston Loveland 41.5|Travis Etienne 43|Cam Skattebo 45|D'Andre Swift 45.5|Terry McLaurin 45.5|Luther Burden III 45.5|David Montgomery 48.5|Lamar Jackson 52.5|Quinshon Judkins 52.5|D.J. Moore 53|Bhayshul Tuten 53.5|Jameson Williams 53.5|Davante Adams 53.5|Mike Evans 53.5|Tyler Warren 55.5|Rome Odunze 56|Christian Watson 56|Bucky Irving 57|Parker Washington 59.5|TreVeyon Henderson 60.5|Drake Maye 63.5|Jadarian Price 64|Jayden Daniels 65.5|Joe Burrow 65.5|Carnell Tate 66.5|Marvin Harrison Jr. 67|Brian Thomas Jr. 67|Jalen Hurts 68.5|Rhamondre Stevenson 69.5"""

def parse(s):
    d = {}
    for chunk in s.split("|"):
        name, val = chunk.rsplit(" ", 1)
        d[key(name)] = float(val)
    return d

ffc, fft = parse(FFC), parse(FFT)

for pl in board:
    k = key(pl["name"])
    a, b = ffc.get(k), fft.get(k)
    vals = [v for v in (a, b) if v is not None]
    pl["ffc"], pl["fft"] = a, b
    pl["adp"] = round(sum(vals) / len(vals), 1) if vals else None

ranked = [p for p in board if p["adp"] is not None]
ranked.sort(key=lambda p: p["adp"])
for i, p in enumerate(ranked, 1):
    p["adp_rank"] = i

vorp_sorted = sorted(board, key=lambda p: -p["vorp"])
for i, p in enumerate(vorp_sorted, 1):
    p["vorp_rank"] = i

out = [p for p in vorp_sorted if p.get("adp_rank")]
for p in out:
    p["edge"] = p["adp_rank"] - p["vorp_rank"]   # +ve = falls later than value

print("{:<4} {:<24} {:<5} {:>7} {:>6} {:>6} {:>6}".format("VR","PLAYER","POS","VORP","ADP","ADPr","EDGE"))
for p in out[:85]:
    print("{:<4} {:<24} {:<5} {:>7.1f} {:>6.1f} {:>6} {:>+6}".format(
        p["vorp_rank"], p["name"], p["posrank"], p["vorp"], p["adp"], p["adp_rank"], p["edge"]))

json.dump(out, open("/home/claude/blended.json","w"), indent=1)
print("\nMissing ADP (not in top-80 of either source):",
      len([p for p in board if p["adp"] is None]))
