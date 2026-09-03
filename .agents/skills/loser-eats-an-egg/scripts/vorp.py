#!/usr/bin/env python3
"""12-team half-PPR VORP board. Projections: FFToday 2026 (standard scoring),
converted to half-PPR (+0.5/rec) and 4-pt pass TD (Sleeper default)."""

import json

# (name, team, std_pts, receptions)
RB = [
 ("Jahmyr Gibbs","DET",295.4,72),("Bijan Robinson","ATL",282.9,84),
 ("Christian McCaffrey","SF",263.9,82),("Jonathan Taylor","IND",250.5,36),
 ("Derrick Henry","BAL",243.6,16),("James Cook","BUF",243.2,32),
 ("Saquon Barkley","PHI",220.3,42),("Kenneth Walker","KC",215.9,42),
 ("Omarion Hampton","LAC",215.5,52),("Kyren Williams","LAR",213.6,34),
 ("De'Von Achane","MIA",211.2,56),("Chase Brown","CIN",210.3,55),
 ("Josh Jacobs","GB",209.2,35),("Ashton Jeanty","LV",208.3,56),
 ("Jeremiyah Love","ARI",203.9,56),("Javonte Williams","DAL",200.3,33),
 ("Breece Hall","NYJ",198.5,47),("Cam Skattebo","NYG",193.4,44),
 ("Bucky Irving","TB",190.0,39),("Bhayshul Tuten","JAC",188.3,38),
 ("D'Andre Swift","CHI",184.5,39),("Travis Etienne","NO",182.9,44),
 ("Quinshon Judkins","CLE",178.3,30),("David Montgomery","HOU",165.9,34),
 ("Tony Pollard","TEN",165.2,32),("Jaylen Warren","PIT",160.3,41),
 ("TreVeyon Henderson","NE",159.3,34),("J.K. Dobbins","DEN",157.2,16),
 ("Jadarian Price","SEA",154.3,31),("Rhamondre Stevenson","NE",151.7,40),
 ("Chuba Hubbard","CAR",144.5,34),("Rico Dowdle","PIT",136.4,30),
 ("Jacory Croskey-Merritt","WAS",129.2,12),("Rachaad White","WAS",126.9,42),
 ("Blake Corum","LAR",119.4,13),("Jordan Mason","MIN",119.3,12),
 ("Kyle Monangai","CHI",117.7,20),("Jonathon Brooks","CAR",117.5,32),
 ("Aaron Jones","MIN",111.0,37),("RJ Harvey","DEN",105.9,44),
 ("Kenneth Gainwell","TB",101.4,37),("Woody Marks","HOU",92.1,27),
 ("Zach Charbonnet","SEA",91.2,19),("Alvin Kamara","NO",90.3,34),
 ("Chris Rodriguez Jr.","JAC",87.2,10),("Isiah Pacheco","DET",80.8,20),
 ("Tyjae Spears","TEN",77.9,38),("Tyrone Tracy Jr.","NYG",77.5,28),
 ("Brian Robinson Jr.","ATL",76.0,9),("Dylan Sampson","CLE",74.1,40),
]

WR = [
 ("Puka Nacua","LAR",223.3,116),("Ja'Marr Chase","CIN",214.5,116),
 ("Jaxon Smith-Njigba","SEA",203.1,105),("Amon-Ra St. Brown","DET",199.1,119),
 ("Drake London","ATL",173.2,99),("Justin Jefferson","MIN",170.8,95),
 ("CeeDee Lamb","DAL",165.7,87),("Nico Collins","HOU",160.6,79),
 ("A.J. Brown","NE",157.5,86),("Chris Olave","NO",156.7,97),
 ("Jameson Williams","DET",155.0,66),("Rashee Rice","KC",153.7,96),
 ("Zay Flowers","BAL",153.6,79),("George Pickens","DAL",152.2,74),
 ("Davante Adams","LAR",151.1,70),("Malik Nabers","NYG",149.7,81),
 ("DeVonta Smith","PHI",148.2,83),("Emeka Egbuka","TB",146.6,78),
 ("Garrett Wilson","NYJ",145.5,100),("Tetairoa McMillan","CAR",144.1,78),
 ("Christian Watson","GB",140.2,65),("Tee Higgins","CIN",138.6,65),
 ("Mike Evans","SF",137.5,69),("Rome Odunze","CHI",136.5,65),
 ("D.J. Moore","BUF",136.0,71),("Jaylen Waddle","DEN",134.7,72),
 ("Luther Burden III","CHI",131.9,70),("Ladd McConkey","LAC",131.6,76),
 ("DK Metcalf","PIT",130.4,67),("Terry McLaurin","WAS",129.7,65),
 ("Marvin Harrison Jr.","ARI",129.3,64),("Courtland Sutton","DEN",126.8,63),
 ("Alec Pierce","IND",121.7,60),("Jayden Reed","GB",118.3,66),
 ("Michael Pittman Jr.","PIT",117.7,76),("Carnell Tate","TEN",116.7,65),
 ("Parker Washington","JAC",111.4,62),("Brian Thomas Jr.","JAC",111.2,56),
 ("Xavier Worthy","KC",111.1,57),("Jakobi Meyers","JAC",110.1,67),
 ("Jordan Addison","MIN",108.8,55),("Chris Godwin","TB",107.9,68),
 ("Quentin Johnston","LAC",106.7,50),("Stefon Diggs","WAS",106.6,62),
 ("Michael Wilson","ARI",106.2,66),("Romeo Doubs","NE",105.9,58),
 ("Khalil Shakir","BUF",100.6,72),("Matthew Golden","GB",100.2,54),
 ("Wan'Dale Robinson","TEN",97.6,77),("KC Concepcion","CLE",95.6,58),
]

TE = [
 ("Brock Bowers","LV",150.8,103),("Trey McBride","ARI",146.9,104),
 ("Colston Loveland","CHI",120.8,73),("Tyler Warren","IND",113.6,78),
 ("Harold Fannin Jr.","CLE",110.1,77),("Kyle Pitts","ATL",109.6,76),
 ("Sam LaPorta","DET",104.5,61),("Tucker Kraft","GB",102.4,57),
 ("Travis Kelce","KC",101.1,73),("Mark Andrews","BAL",100.5,61),
 ("Dallas Goedert","PHI",98.5,58),("George Kittle","SF",95.5,57),
 ("Dalton Kincaid","BUF",94.5,52),("Hunter Henry","NE",92.4,55),
 ("Isaiah Likely","NYG",89.6,56),("Jake Ferguson","DAL",88.5,71),
 ("Pat Freiermuth","PIT",86.6,53),("Juwan Johnson","NO",85.6,60),
 ("Greg Dulcich","MIA",84.6,54),("Dalton Schultz","HOU",82.6,67),
 ("Brenton Strange","JAC",81.1,51),("Terrance Ferguson","LAR",77.0,40),
 ("T.J. Hockenson","MIN",74.8,59),("AJ Barner","SEA",74.4,54),
]

# (name, team, fftoday_pts, pass_TDs, INTs) -> 4-pt pass TD, -1 per INT (league setting)
QB_RAW = [
 ("Josh Allen","BUF",422.1,26,9),("Drake Maye","NE",384.3,29,9),
 ("Lamar Jackson","BAL",371.8,26,8),("Jayden Daniels","WAS",368.7,23,11),
 ("Jalen Hurts","PHI",363.4,23,7),("Joe Burrow","CIN",362.6,33,10),
 ("Bo Nix","DEN",360.7,27,12),("Dak Prescott","DAL",358.6,30,10),
 ("Brock Purdy","SF",358.6,26,13),("Jared Goff","DET",356.0,32,10),
 ("Caleb Williams","CHI",354.8,27,8),("Justin Herbert","LAC",352.3,25,11),
 ("Trevor Lawrence","JAC",351.5,25,11),("Jaxson Dart","NYG",350.9,20,9),
 ("Tyler Shough","NO",347.3,22,10),("Matthew Stafford","LAR",346.3,33,9),
 ("Patrick Mahomes","KC",345.1,26,11),("Kyler Murray","MIN",343.0,23,12),
 ("Sam Darnold","SEA",329.0,26,13),("Baker Mayfield","TB",326.1,24,12),
 ("Malik Willis","MIA",322.2,15,10),("Jordan Love","GB",321.3,26,10),
 ("Daniel Jones","IND",314.2,21,12),("C.J. Stroud","HOU",299.8,21,10),
 ("Bryce Young","CAR",296.7,22,10),("Geno Smith","NYJ",286.7,21,14),
 ("Cam Ward","TEN",285.8,19,11),("Aaron Rodgers","PIT",282.5,23,8),
 ("Jacoby Brissett","ARI",282.1,19,10),
]

def half(rows, pos):
    return sorted(
        [{"name": n, "team": t, "pos": pos, "pts": round(p + 0.5 * r, 1)} for n, t, p, r in rows],
        key=lambda x: -x["pts"])

rbs, wrs, tes = half(RB, "RB"), half(WR, "WR"), half(TE, "TE")
qbs = sorted([{"name": n, "team": t, "pos": "QB", "pts": round(p - 2 * td + 1 * i, 1)}
              for n, t, p, td, i in QB_RAW], key=lambda x: -x["pts"])

# --- Replacement levels -------------------------------------------------
# 12 teams x (1QB, 2RB, 2WR, 1TE, 2FLEX). 24 flex slots in a half-PPR league
# skew RB-heavy: assume 13 RB / 10 WR / 1 TE fill them.
LEVELS = {"QB": 12, "RB": 24 + 13, "WR": 24 + 10, "TE": 12 + 1}
pools = {"QB": qbs, "RB": rbs, "WR": wrs, "TE": tes}
repl = {p: pools[p][LEVELS[p]]["pts"] for p in LEVELS}   # next man up = index N

board = []
for p, pool in pools.items():
    for i, pl in enumerate(pool, 1):
        pl["posrank"] = f"{p}{i}"
        pl["vorp"] = round(pl["pts"] - repl[p], 1)
        board.append(pl)
board.sort(key=lambda x: -x["vorp"])

print("REPLACEMENT LEVELS (half-PPR pts)")
for p in ["QB", "RB", "WR", "TE"]:
    print(f"  {p}: {repl[p]:.1f}  (= {pools[p][LEVELS[p]]['name']}, the {LEVELS[p]+1}th {p})")

print("\n{:<4} {:<24} {:<4} {:<6} {:>7} {:>7}".format("#", "PLAYER", "TM", "POS", "PTS", "VORP"))
for i, pl in enumerate(board[:110], 1):
    print("{:<4} {:<24} {:<4} {:<6} {:>7.1f} {:>7.1f}".format(
        i, pl["name"], pl["team"], pl["posrank"], pl["pts"], pl["vorp"]))

json.dump(board, open("/home/claude/board.json", "w"), indent=1)
