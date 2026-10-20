"""Hand-written analysis for the trebevicka23 draft page (build_draft_page.py). Every number comes from
"Draft Analysis/trebevicka23_draft.json" (draft_analysis.py) or the fact-checked news notes in it.
Pick numbers follow the league's snake with third-round reversal (R1 1->12, R2 and R3 12->1, then alternating)."""

NOTES = {
 "Bobulini Direktori": {
  "verdict": "The No. 1 pick paid off. Jokić is projected 6.5 FP/G ahead of anyone else, the late rounds were the best in the draft, and the roster has the most guard volume in the league.",
  "strong": [
   "Nikola Jokić projects at 54.5 FP/G. The next best player in the league (Giannis) is at 48.0.",
   "Late-round value: Collin Sexton at pick 133 is ranked 111th, Jeremiah Fears at 132 is ranked 112th, and Keldon Johnson at 156 (the last pick) is ranked 135th.",
   "Seven PG-eligible players, so a guard slot is filled every night.",
  ],
  "weak": [
   "Only three C-eligible players (Jokić, Ware, Collins) and three forwards. The roster leans heavily on guards.",
   "That guard surplus wastes about 476 fantasy points over the season, because players with games end up on the bench. That's the most in the league, so trading two guards for one forward would help.",
   "Brandon Miller went 37th but is ranked 61st. Knueppel and Sexton are day-to-day.",
  ],
 },
 "Jokers": {
  "verdict": "The strongest frontcourt in the league and the second-best full-strength lineup. The risk is age and injuries on the back half of the roster.",
  "strong": [
   "Giannis (48.0 FP/G) and Sabonis (35.9) are both top-12 projected players. Sabonis at pick 22 is ranked 12th.",
   "Good value: James Harden at pick 39 is ranked 30th, Nic Claxton at 111 is ranked 101st, and Quentin Grimes at 135 is ranked 118th.",
   "Six C-eligible players, so both C slots are covered all season.",
  ],
  "weak": [
   "Paul George went 82nd but is ranked 115th, and is projected for only 59 games.",
   "Kyrie Irving is coming back from his ACL tear (62.7 projected games), and Harden turns 37 this season.",
   "Two of the last four picks start the season hurt. Claxton has a hamstring strain with no timeline, and Nurkić is out at least four weeks.",
  ],
 },
 "GrobariTiket": {
  "verdict": "A very safe build. SGA is the most durable star here (75 projected games), and almost no points are lost to the bench.",
  "strong": [
   "Shai Gilgeous-Alexander projects at 44.0 FP/G over 75 games.",
   "Strong middle rounds: Jarrett Allen at pick 64 is ranked 48th, and Mikal Bridges (81 projected games) at 88 is ranked 76th.",
   "Ties for the most lineup games (919), and only 186 points are lost to the bench.",
  ],
  "weak": [
   "Only two PG-eligible players (SGA and Ja Morant). If either one misses time, the G slot thins out.",
   "Bam Adebayo (groin tightness) and Santi Aldama are day-to-day going into the opener.",
   "The full-strength lineup (286.1 FP/G) ranks 5th, so this team wins by staying healthy rather than with a high ceiling.",
  ],
 },
 "Leka's laying hens": {
  "verdict": "The highest ceiling in the league: 301.2 FP/G at full strength, the best of all 12 teams. It also has the most injury risk among the top teams.",
  "strong": [
   "Wembanyama, Anthony Davis, Booker and Curry make up the best top four in the draft.",
   "The best value pick of the draft: Brandon Ingram at pick 128 is ranked 87th once he's healthy. Jakob Poeltl at 137 is ranked 122nd.",
   "Devin Booker at pick 32 is ranked 22nd, and Stephen Curry at 41 is ranked 29th.",
  ],
  "weak": [
   "Brandon Ingram has a partially torn Achilles and won't be ready for the opener. Anthony Davis is projected for only 63 games.",
   "Six C-eligible players but only three SF/PF. Davis, Sarr and Ayton all play for the Wizards, so they compete for the same minutes.",
   "One of the lowest totals of lineup games (873). Ryan Rollins went 89th but is ranked 105th.",
  ],
 },
 "Cheva's Wolves": {
  "verdict": "A deep group of star forwards with good value picks, but the oldest core in the league and only two centers.",
  "strong": [
   "Tatum, Durant, Franz Wagner and LeBron James are four forwards who each project at 30.8+ FP/G.",
   "LeBron at pick 55 is ranked 37th, Durant at 31 is ranked 20th, Kuminga at 138 is ranked 113th, and Mikel Brown Jr. at 151 is ranked 128th.",
   "911 lineup games, near the top of the league.",
  ],
  "weak": [
   "Only two C-eligible players (Duren and Gobert). Whenever either one is out, a C slot sits empty.",
   "Age: LeBron, Durant and DeRozan are all 37 or older, and Tatum is coming back from a ruptured Achilles.",
   "Franz Wagner had right shoulder soreness in the preseason finale. VanVleet, back from his ACL tear, went 114th but is ranked 134th.",
  ],
 },
 "MuslimSons": {
  "verdict": "Young and durable, with plenty of games, but no superstar at the top.",
  "strong": [
   "Cooper Flagg (35.5 FP/G, 74 games) and Amen Thompson (76 games) are reliable every night.",
   "Value picks: Zion Williamson at 53 is ranked 41st, VJ Edgecombe at 101 is ranked 89th, and Myles Turner at 149 is ranked 138th.",
   "919 lineup games (tied for the most) and only 206 points lost to the bench.",
  ],
  "weak": [
   "No player above Flagg's 35.5 FP/G. Only the RAVENS have a lower best player.",
   "Center depth is thin: Zion, Mamukelashvili and Turner.",
   "Naji Marshall went 125th but is ranked 166th, and Cedric Coward went 77th but is ranked 91st. Dejounte Murray is day-to-day.",
  ],
 },
 "VrH Chewbacca's": {
  "verdict": "A strong top three, but a roster built with too many point guards.",
  "strong": [
   "Cade Cunningham (38.1 FP/G), Karl-Anthony Towns (36.0) and Josh Giddey (34.2, ranked 23rd but taken 28th).",
   "CJ McCollum at 100 is ranked 77th, and Miles Bridges at 76 is ranked 63rd.",
  ],
  "weak": [
   "Six PG-eligible players and only two centers (Towns and Kessler). The 404 points lost to the bench is the 2nd-highest in the league.",
   "Reaches: Walker Kessler at 52 is ranked 69th, Davion Mitchell at 148 is ranked 165th, and Ajay Mitchell at 124 is ranked 182nd.",
   "Jerami Grant is day-to-day.",
  ],
 },
 "Milos's RAVENS": {
  "verdict": "A solid, balanced roster that plays a lot of games, but it lacks a player who wins weeks on his own.",
  "strong": [
   "Şengün and Banchero (34.1 and 34.6 FP/G) anchor the frontcourt.",
   "Brandin Podziemski at pick 95 is ranked 82nd, and Anthony Black at 146 is ranked 139th.",
   "917 lineup games. Podziemski is projected for 78.",
  ],
  "weak": [
   "The 2nd-lowest full-strength lineup (271.2 FP/G), with no one above 35 FP/G.",
   "Trae Young went 26th but is ranked 36th, and Jaren Jackson Jr. went 74th but is ranked 85th. The wings are thin, with only three SF-eligible players.",
   "Coby White and Anthony Black are day-to-day.",
  ],
 },
 "Velja's Hajduks": {
  "verdict": "Jalen Johnson and Brunson are a strong start, but several early reaches put this team in the middle of the pack.",
  "strong": [
   "Jalen Johnson projects at 40.3 FP/G, and Brunson at 34.5 FP/G over 73 games.",
   "Good late wings: Norman Powell at pick 91 is ranked 70th, and Dyson Daniels at 102 is ranked 79th.",
   "Only 206 points are lost to the bench.",
  ],
  "weak": [
   "Haliburton (pick 30, ranked 47th, back from his Achilles tear) and Trey Murphy (pick 43, ranked 57th) were both taken early.",
   "Zach Edey at 78 is ranked 102nd, and Khaman Maluach at 150 is ranked 205th.",
   "Only two PG-eligible players, and the centers (Clingan 25.9, Edey 24.8 FP/G) add little fantasy production.",
  ],
 },
 "CXABBHE": {
  "verdict": "A good team on paper (6th-best full-strength lineup), but it depends more than anyone on players with injury histories.",
  "strong": [
   "Damian Lillard at pick 49 is ranked 44th, and Darryn Peterson at 97 is ranked 86th. Scottie Barnes is C-eligible, which matters on a roster with two centers.",
   "284.2 FP/G when everyone is healthy.",
  ],
  "weak": [
   "Embiid (54 projected games), Kawhi (63.7) and Lillard (back from his Achilles tear) leave only 875 lineup games, the 2nd-fewest in the league.",
   "The back-to-back picks at the turn were reaches: Avdija at 12 is ranked 24th, and Kawhi at 13 is ranked 34th. Ty Jerome at 72 is ranked 95th.",
   "Only two C-eligible players (Embiid and Barnes).",
  ],
 },
 "Andrija's Gamblers": {
  "verdict": "A good first three rounds. From round 6 on, six picks went 10+ spots early, the most in the league.",
  "strong": [
   "Anthony Edwards, Tyrese Maxey and Evan Mobley are a strong top three.",
   "Michael Porter Jr. at 51 is ranked 45th, and late picks Ayo Dosunmu (123, ranked 103rd) and P.J. Washington (147, ranked 129th) are value.",
   "Only 141 points lost to the bench, the lowest in the league.",
  ],
  "weak": [
   "The lowest full-strength lineup in the league (269.2 FP/G).",
   "Reaches: Harper (−20), Gordon (−33), McDaniels (−15), Murray-Boyles (−81), and Hugo González at pick 142 (9.8 FP/G projected, ranked 275th, the 2nd-biggest reach in the draft, although he averaged 20.6 FP/G in preseason).",
   "Chet Holmgren is day-to-day.",
  ],
 },
 "Dominators": {
  "verdict": "Luka is an elite first pick, but the bottom of the roster is the weakest in the league and costs games all season.",
  "strong": [
   "Luka Dončić projects at 46.7 FP/G, 4th best in the league. Reaves, Siakam and Zubac are solid starters.",
   "AJ Dybantsa at pick 86 is ranked 64th, and Pascal Siakam at 35 is ranked 31st.",
  ],
  "weak": [
   "The fewest lineup games in the league (871).",
   "Ben Simmons at pick 131 is the biggest reach in the draft (8.6 FP/G, 26 projected games). Maxime Raynaud is out (knee), and Lendeborg (−45) and Sharpe (−18) were also reaches.",
   "Buzelis at pick 59 is ranked 71st. Dybantsa sprained his ankle in the preseason finale.",
  ],
 },
}
