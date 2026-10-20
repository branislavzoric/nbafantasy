"""Hand-written analysis for the Boogie Nights draft page (build_draft_page.py --league boogie). Every number comes from
"Draft Analysis/boogie_draft.json" (draft_analysis.py --league boogie) or the fact-checked news notes in it.
Pick numbers follow the league's snake with third-round reversal (R1 1->12, R2 and R3 12->1, then alternating)."""

NOTES = {
 "Cheva": {
  "verdict": "The No. 1 pick paid off. Jokić is projected 7.8 FP/G ahead of anyone else, and the roster has the most projected season points in the league by more than 1,000.",
  "strong": [
   "Nikola Jokić projects at 60.2 FP/G under Boogie scoring. The next best player in the league (Giannis) is at 52.4.",
   "Value all the way down: Devin Booker at pick 36 is ranked 18th, Darius Acuff Jr. at 108 is ranked 77th, and Ace Bailey at 156 (the last pick) is ranked 138th.",
   "Mikal Bridges is projected for 81 games and Derrick White for 76, so the wings rarely leave a slot empty.",
  ],
  "weak": [
   "Only three C-eligible players (Jokić, Poeltl, P.J. Washington). If Jokić misses time, both C slots get thin.",
   "Five PG-eligible and six PF-eligible players means 311 points are lost to the bench, the second most in the league.",
   "Yaxel Lendeborg went 109th but is ranked 148th, and Ty Jerome went 84th but is ranked 92nd.",
  ],
 },
 "Djole": {
  "verdict": "SGA plus four starting-quality centers. It has the second-best season total, and Embiid's health decides how close it gets to first.",
  "strong": [
   "Shai Gilgeous-Alexander projects at 48.6 FP/G over 75 games.",
   "Four C-eligible players (Mobley, Zubac, Embiid, Gobert), so both C slots are covered all season.",
   "Great rookie value: Darryn Peterson at pick 113 is ranked 85th, VJ Edgecombe at 104 is ranked 84th, and AJ Dybantsa at 80 is ranked 64th.",
  ],
  "weak": [
   "Joel Embiid projects at 41.0 FP/G but for only 54 games.",
   "Ajay Mitchell went 137th but is ranked 187th, and Grayson Allen went 152nd but is ranked 173rd.",
   "Only two PF-eligible players (Mobley and Barrett). Dybantsa sprained his ankle in the preseason finale, and Grayson Allen is day-to-day.",
  ],
 },
 "Andrija": {
  "verdict": "Luka plus the best late rounds in the draft. The full-strength lineup is only the 6th best, so the team relies on depth and durability.",
  "strong": [
   "Luka Dončić projects at 51.9 FP/G, the 3rd best in the league. Kevin Durant at pick 35 is ranked 22nd, and Josh Giddey at 23 is ranked 17th.",
   "Late-round value: Mikel Brown Jr. at pick 155 is ranked 124th, Derik Queen at 110 is ranked 87th, and Isaiah Hartenstein at 131 is ranked 109th.",
   "Five C-eligible players, and 918 lineup games (tied for 2nd most).",
  ],
  "weak": [
   "Collin Murray-Boyles went 86th but is ranked 174th, the 3rd-biggest reach in the draft. Buzelis (−12) and Okongwu (−10) also went early.",
   "Only three PG-eligible players (Luka, Giddey, Mikel Brown Jr.).",
   "288 points are lost to the bench, the 3rd most, because of six PF-eligible and five C-eligible players.",
  ],
 },
 "Bane": {
  "verdict": "The best full-strength lineup in the league (337.9 FP/G), but also the fewest lineup games (858). At full strength this is a title team; the IL and the bench decide where it finishes.",
  "strong": [
   "Five top-50 picks (Şengün, Mitchell, Reaves, Curry, Zion), all projected at 35.8 FP/G or better, so there's no weak starter.",
   "Value picks: Brandon Ingram at pick 120 is ranked 94th, Norman Powell at 97 is ranked 79th, Stephen Curry at 48 is ranked 34th, and De'Aaron Fox at 72 is ranked 58th.",
   "Every position is covered (5 PG, 5 SG, 5 SF, 6 PF, 4 C eligible), and only 194 points are lost to the bench, the 2nd fewest.",
  ],
  "weak": [
   "Injuries to start the season: Ingram (partially torn Achilles) and Nurkić (toe, out at least four weeks from Oct 9) are out, and Curry and Jerami Grant are day-to-day.",
   "Paul George went 96th but is ranked 115th, and is projected for only 59 games. Zion is projected for 65.7.",
   "The season total (27,256) is 1 point behind Andrija. Teams 2 to 5 are separated by about 110 points, so this is effectively a tie for second.",
  ],
 },
 "Ruzic": {
  "verdict": "Wembanyama and Banchero are a strong top two, with good value in the middle rounds. The last four picks add little.",
  "strong": [
   "Victor Wembanyama projects at 49.8 FP/G, the 4th best in the league.",
   "LeBron James at pick 58 is ranked 37th, Coby White at 82 is ranked 70th, and Jarrett Allen at 63 is ranked 52nd.",
   "Sandro Mamukelashvili at pick 130 is ranked 116th, although only one source projects him.",
  ],
  "weak": [
   "Kyshawn George went 111th but is ranked 136th, Davion Mitchell went 135th but is ranked 153rd, and Naji Marshall went 154th but is ranked 179th.",
   "Nic Claxton has a hamstring strain with no timeline, and Coby White is day-to-day.",
   "Only three SG-eligible players, and LeBron is projected for 66 games.",
  ],
 },
 "Rakic": {
  "verdict": "Giannis and Duren are the best center pair in the league, and the guard picks were good. Most of the back third were reaches.",
  "strong": [
   "Giannis Antetokounmpo projects at 52.4 FP/G, the 2nd best in the league. Jalen Duren at pick 21 is ranked 21st.",
   "Guard value: Kyrie Irving at pick 64 is ranked 51st, Darius Garland at 57 is ranked 46th, and James Harden at 33 is ranked 28th.",
   "Six SF-eligible players, so the wing slots stay filled.",
  ],
  "weak": [
   "Bennedict Mathurin went 136th but is ranked 170th, Rui Hachimura went 112th but is ranked 137th, and Jaden McDaniels went 88th but is ranked 103rd.",
   "Only three C-eligible players (Giannis, Duren, Turner) and three PF-eligible players.",
   "Kyrie Irving is coming back from his ACL tear (62.7 projected games), and Franz Wagner had shoulder soreness in the preseason finale.",
  ],
 },
 "Meda": {
  "verdict": "Cade and Sabonis are a strong top two, and the roster is durable. The full-strength lineup (319.2 FP/G) is only the 7th best.",
  "strong": [
   "Domantas Sabonis at pick 19 is ranked 12th. Cade Cunningham projects at 43.6 FP/G.",
   "Value picks: DeMar DeRozan at 114 is ranked 91st (76 projected games), Miles Bridges at 90 is ranked 68th, and Jeremiah Fears at 127 is ranked 108th.",
   "913 lineup games, and only 209 points are lost to the bench.",
  ],
  "weak": [
   "Zaccharie Risacher went 151st but is ranked 230th, although only one source projects him. John Collins went 79th but is ranked 101st.",
   "Caleb Wilson (−11), Tyler Herro (−8) and Ja Morant (−8) also went earlier than their rank.",
   "Only three C-eligible players (Sabonis, Siakam, Collins), and Josh Hart is day-to-day.",
  ],
 },
 "Gari": {
  "verdict": "Tatum is back, and the roster plays the most games in the league. But too many similar guards make it the team that loses the most points to the bench.",
  "strong": [
   "Jayson Tatum projects at 44.0 FP/G, and the Boston Globe says his Achilles recovery is complete.",
   "The best value pick of the draft: Ayo Dosunmu at pick 139 is ranked 105th.",
   "The most lineup games in the league (929).",
  ],
  "weak": [
   "355 points are lost to the bench, the most in the league, because there are seven SG-eligible and five PG-eligible players.",
   "The full-strength lineup (305.0 FP/G) is the 2nd lowest. Ryan Rollins went 78th but is ranked 102nd.",
   "Only three C-eligible players (Barnes, Jabari Smith Jr., Wendell Carter Jr.).",
  ],
 },
 "Oskar": {
  "verdict": "Jalen Johnson was a great pick, but Kawhi at 16 and a thin end of the roster leave the team in the bottom four.",
  "strong": [
   "Jalen Johnson at pick 9 is ranked 6th (44.6 FP/G).",
   "Value picks: Dejounte Murray at pick 76 is ranked 65th, and Quentin Grimes at 141 is ranked 119th.",
   "918 lineup games (tied for 2nd most). Amen Thompson is projected for 76 games.",
  ],
  "weak": [
   "Kawhi Leonard went 16th but is ranked 36th, and is projected for only 63.7 games.",
   "Hannes Steinbach at pick 148 is the biggest reach in the draft (ranked 242nd). Neemias Queta (−33) and Toumani Camara (−23) also went early.",
   "Only three PG-eligible players, and Dejounte Murray is day-to-day.",
  ],
 },
 "Petar": {
  "verdict": "Two Knicks and Anthony Davis at the top. It loses the fewest points to the bench, but the lineup tops out at 314.2 FP/G.",
  "strong": [
   "Anthony Davis at pick 26 is ranked 19th (43.0 FP/G).",
   "CJ McCollum at pick 98 is ranked 81st, and Anthony Black at 143 is ranked 134th.",
   "Only 185 points are lost to the bench, the fewest in the league. Six PG-eligible and five C-eligible players cover every slot.",
  ],
  "weak": [
   "Reaches on bigs: Zach Edey went 74th but is ranked 98th, Day'Ron Sharpe went 95th but is ranked 123rd, and Jalen Smith went 146th but is ranked 198th.",
   "Davis is projected for 63 games, and Damian Lillard (back from the ruptured Achilles) for 66.3.",
   "Dylan Harper went 71st but is ranked 88th, and Anthony Black is day-to-day.",
  ],
 },
 "Rade": {
  "verdict": "Edwards, Avdija and Bam are a solid top three, but 10 of the 13 picks went earlier than their consensus rank, and the full-strength lineup is the lowest in the league.",
  "strong": [
   "Anthony Edwards projects at 40.0 FP/G over 74 games. Bam Adebayo at pick 27 is ranked 23rd.",
   "Devin Vassell at pick 142 is ranked 140th, so he's fair value as the last guard.",
   "907 lineup games, and only 199 points are lost to the bench.",
  ],
  "weak": [
   "The lowest full-strength lineup in the league (303.9 FP/G).",
   "Reaches: Ausar Thompson (−23), Jalen Suggs (−21), Aaron Gordon (−14), Payton Pritchard (−13), and Avdija and Holmgren (−10 each).",
   "Bam (groin tightness) and Chet Holmgren are day-to-day.",
  ],
 },
 "Veljko": {
  "verdict": "Flagg and Maxey are a great start, but the last two picks were among the biggest reaches in the draft, and the team has the lowest season total.",
  "strong": [
   "Cooper Flagg projects at 39.6 FP/G, and Tyrese Maxey at pick 17 is ranked 15th.",
   "Kon Knueppel at pick 68 is ranked 57th, and Andrew Nembhard and Saddiq Bey both went later than their rank.",
   "Seven SG-eligible and six SF-eligible players, so the wings are covered.",
  ],
  "weak": [
   "Isaiah Jackson at pick 149 (ranked 241st) and Khaman Maluach at 140 (ranked 207th) are the 2nd- and 5th-biggest reaches in the draft.",
   "Walker Kessler went 44th but is ranked 63rd. Haliburton (−12, back from the Achilles tear) and Kel'el Ware (−12) also went early.",
   "Only three PG-eligible and three PF-eligible players, and Knueppel is day-to-day.",
  ],
 },
}
