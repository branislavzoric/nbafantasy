#!/usr/bin/env python3
"""
combo_model.py

Prices football combo bets ("1X&I1-3&II1-3", "GG&3+", "1-1&4+", "PG1&3+", ...) with a goals model.

Model: home and away goals in each half are independent Poisson counts, 45% of the expected goals in the
first half, with a Dixon-Coles correction on low full-time scores (0-0, 1-0, 0-1, 1-1) so draws aren't
underpriced. Per match, (home xG, away xG, rho) are fitted so the model reproduces the books' no-vig
1X2 and Total 2.5 probabilities. A combo's probability is the sum over every half-by-half score
(x1, y1, x2, y2) that satisfies all its conditions. "Home scores first" uses the fact that, given the
goal counts in a half, the order of the goals is random: P = home goals / goals, in the first half
that has a goal.

parse(text, ctx, book) turns a bookmaker's selection text into conditions (None if it can't be priced,
e.g. goal-minute bets). Notation: 1 / X / 2 result, 1X / 12 / X2 double chance, a-b or a+ goals,
prefix I / II = first / second half, D / G / T1: / T2: = home / away goals, GG / NG, r-r half time -
full time, DP1 = wins both halves, HP1 = wins by 2+, PG1 = scores first, I<II / I=II / I>II goals per
half, NE = the whole combo does NOT happen.
"""

import math
import re

import numpy as np

MAXG = 8                   # goals per team per half on the grid
H1_SHARE = 0.45            # share of expected goals in the first half

# -----------------------------------------------------------------------------
# PARSER
# -----------------------------------------------------------------------------

TEAM_CTX = re.compile(r"(?i)(tim\s*([12])|doma[cć]in|gost)\s+(daje gol|ukupno gol)")
HTFT_CTX = re.compile(r"(?i)poluvreme\s*-\s*kraj|poluvreme/kraj|na poluvremenu|^\s*[12X]-[12X] kombin|1-2, 2-1|"
                      r"\bPK_|\bDW_|poluvremenu\s+i\s+na\s+kraju")
MINUTES = re.compile(r"(?i)minut|<=\s*\d|>\s*\d\d")
ATOMS = [
    (re.compile(r"DP([12])"), lambda m: ("dp", m[1])),
    (re.compile(r"(?:HP|Hend)([12])"), lambda m: ("hp", m[1])),
    (re.compile(r"(?:PDG|PG|Tim)([12])$"), lambda m: ("pg", m[1])),
    (re.compile(r"HF:([12X])-([12X])"), lambda m: ("htft", m[1], m[2])),
    (re.compile(r"I<II|vg2"), lambda m: ("cmp", "<")),
    (re.compile(r"vgX"), lambda m: ("cmp", "=")),
    (re.compile(r"vg1"), lambda m: ("cmp", ">")),
    (re.compile(r"I=II"), lambda m: ("cmp", "=")),
    (re.compile(r"II>|IIpol"), lambda m: ("cmp", "<")),
    (re.compile(r"I>II|I>|Ipol"), lambda m: ("cmp", ">")),
    (re.compile(r"(II|I)?(GG|NG)"), lambda m: ("gg", m[1] or "", m[2] == "GG")),
    (re.compile(r"(T[12]:|D|G)?(II|I)?(\d+)\+"), lambda m: ("tot", m[2] or "", m[1], int(m[3]), 99)),
    (re.compile(r"([12X])-([12X])(?=&|$)"), lambda m: ("htft?", m[1], m[2])),
    (re.compile(r"(T[12]:|D|G)?(II|I)?(\d+)-(\d+)"), lambda m: ("tot", m[2] or "", m[1], int(m[3]), int(m[4]))),
    (re.compile(r"(II|I)?(1X|12|X2)(?![\d+\-])"), lambda m: ("res", m[1] or "", set(m[2]))),
    (re.compile(r"(II|I)?([12X])(?![\d+\-])"), lambda m: ("res", m[1] or "", {m[2]})),
]


def _normalise(s):
    s = s.replace("–", "-").strip()
    neg = False
    m = re.match(r"(?i)^ne\b\s*(.*)$", s) or re.match(r"(?i)^ne(\(.*)$", s)
    if m:
        neg, s = True, m[1].strip()
    s = re.sub(r"T1\s*&\s*T2\s+(\S+)", r"T1:\1&T2:\1", s)                    # Mozzart "T1&T2 2+"
    s = re.sub(r"(?i)\bTim\s*([12])\s+(?=\d)", r"T\1:", s)                    # Admiral "Tim1 1+"
    s = re.sub(r"\bT([12])\s+(?=\d)", r"T\1:", s)                             # Mozzart "T1 2-3"
    s = re.sub(r"(\d+(?:\+|-\d+))\s*(?:Tim|T)([12])\b", r"T\2:\1", s)         # Admiral "3+Tim1", "2+T1"
    s = re.sub(r"(\d+(?:\+|-\d+))(II|I)\b", r"\2\1", s)                       # Admiral "1+I", "1-3II"
    s = re.sub(r"(?i)\b(GG|NG)(II|I)\b", r"\2\1", s)                          # Admiral "GGI"
    s = re.sub(r"\b(gg|ng)\b", lambda m: m[1].upper(), s)
    s = re.sub(r"\b([12])([DG])(?=\d)", lambda m: m[2] + ("I" if m[1] == "1" else "II"), s)   # MaxBet "1G1+"
    s = re.sub(r"\b(GG|NG)([12])\b", lambda m: ("I" if m[2] == "1" else "II") + m[1], s)      # MaxBet "GG1"
    s = re.sub(r"\b([12])P(?=\d)", lambda m: "I" if m[1] == "1" else "II", s)                 # MaxBet "1P0-1", "2P1"
    s = re.sub(r"\bP([12X])\b", r"I\1", s)                                                     # MaxBet "P1"
    s = re.sub(r"\b([12])>", lambda m: "I>" if m[1] == "1" else "II>", s)                     # MaxBet "1>"
    s = re.sub(r"[()\s]", "", s)
    return neg, s


def parse(text, ctx="", book=""):
    """-> (neg, [atoms]) or None. ctx = market name and/or long label (team / half-time hints)."""
    if not text or "&" not in text:                                         # single-part "NE 1" etc. are ambiguous
        return None
    if MINUTES.search(ctx or "") or MINUTES.search(text):                      # goal-minute bets: can't price
        return None
    tm = TEAM_CTX.search(ctx or "")
    subject = None
    if tm:
        subject = "H" if (tm[2] == "1" or tm[1].lower().startswith("doma")) else "A"
    if book in ("soccerbet", "maxbet") and ctx:                               # long label says whose goals
        lab = ctx.split(" | ")[-1]
        if "ukupno golova" not in lab.lower() and "prvi gol" not in lab.lower():
            if lab.startswith(("Domaćin postiže", "$h daje", "$h postiže")):
                subject = "H"
            elif lab.startswith(("Gost postiže", "$a daje", "$a postiže")):
                subject = "A"
    htft_ok = bool(HTFT_CTX.search(ctx or ""))
    neg, s = _normalise(text)
    atoms = []
    for ci, chunk in enumerate(c for c in s.split("&") if c):
        pos = 0
        while pos < len(chunk):
            for rx, make in ATOMS:
                m = rx.match(chunk, pos)
                if not m:
                    continue
                a = make(m)
                if a[0] == "htft?":
                    if "X" in a[1:] or (htft_ok and ci == 0) or (a[1], a[2]) == ("2", "1"):
                        a = ("htft", a[1], a[2])
                    else:
                        continue                                              # it's a goal range
                if a[0] == "tot":
                    who = {"T1:": "H", "D": "H", "T2:": "A", "G": "A"}.get(a[2]) or subject or "T"
                    a = ("tot", a[1], who, a[3], a[4])
                atoms.append(a)
                pos = m.end()
                break
            else:
                return None
    return (neg, atoms) if atoms else None


def key(parsed):
    """Canonical string for a parsed combo, so the same bet at different books lines up."""
    neg, atoms = parsed
    parts = []
    for a in atoms:
        if a[0] == "res":
            parts.append(f"{a[1]}{''.join(sorted(a[2], key='1X2'.index))}")
        elif a[0] == "tot":
            parts.append(f"{a[1]}{'' if a[2] == 'T' else a[2]}{a[3]}{'+' if a[4] == 99 else '-' + str(a[4])}")
        elif a[0] == "gg":
            parts.append(f"{a[1]}{'GG' if a[2] else 'NG'}")
        elif a[0] == "htft":
            parts.append(f"{a[1]}/{a[2]}")
        else:
            parts.append(f"{a[0]}{a[1]}")
    return ("NOT " if neg else "") + "&".join(sorted(parts))

# -----------------------------------------------------------------------------
# GOALS MODEL
# -----------------------------------------------------------------------------

_g = np.arange(MAXG + 1)
X1, Y1, X2, Y2 = (a.ravel() for a in np.meshgrid(_g, _g, _g, _g, indexing="ij"))
HX, AX = X1 + X2, Y1 + Y2


def _pois(lam):
    return np.array([math.exp(-lam) * lam ** k / math.factorial(k) for k in _g])


def _res(h, a):
    return np.where(h > a, "1", np.where(h == a, "X", "2"))


RES = {"": _res(HX, AX), "I": _res(X1, Y1), "II": _res(X2, Y2)}
SCOPE = {"": (HX, AX), "I": (X1, Y1), "II": (X2, Y2)}


class Match:
    def __init__(self, lam_h, lam_a, rho=0.0):
        self.lam_h, self.lam_a, self.rho = lam_h, lam_a, rho
        f = H1_SHARE
        p = (_pois(lam_h * f)[X1] * _pois(lam_a * f)[Y1] * _pois(lam_h * (1 - f))[X2] * _pois(lam_a * (1 - f))[Y2])
        tau = np.ones_like(p)                                             # Dixon-Coles on full-time score
        tau[(HX == 0) & (AX == 0)] = 1 - lam_h * lam_a * rho
        tau[(HX == 1) & (AX == 0)] = 1 + lam_a * rho
        tau[(HX == 0) & (AX == 1)] = 1 + lam_h * rho
        tau[(HX == 1) & (AX == 1)] = 1 - rho
        p = p * np.clip(tau, 0, None)
        self.p = p / p.sum()
        self._cache = {}

    def _atom(self, a):
        kind = a[0]
        if kind == "res":
            return np.isin(RES[a[1]], list(a[2]))
        if kind == "tot":
            h, aw = SCOPE[a[1]]
            n = {"T": h + aw, "H": h, "A": aw}[a[2]]
            return (n >= a[3]) & (n <= a[4])
        if kind == "gg":
            h, aw = SCOPE[a[1]]
            both = (h > 0) & (aw > 0)
            return both if a[2] else ~both
        if kind == "htft":
            return (RES["I"] == a[1]) & (RES[""] == a[2])
        if kind == "dp":
            return (RES["I"] == a[1]) & (RES["II"] == a[1])
        if kind == "hp":
            return (HX - AX >= 2) if a[1] == "1" else (AX - HX >= 2)
        if kind == "cmp":
            g1, g2 = X1 + Y1, X2 + Y2
            return {"<": g1 < g2, "=": g1 == g2, ">": g1 > g2}[a[1]]
        if kind == "pg":
            n1, n2 = X1 + Y1, X2 + Y2
            mine1, mine2 = (X1, X2) if a[1] == "1" else (Y1, Y2)
            with np.errstate(divide="ignore", invalid="ignore"):
                return np.where(n1 > 0, mine1 / np.maximum(n1, 1), np.where(n2 > 0, mine2 / np.maximum(n2, 1), 0.0))
        raise ValueError(a)

    def prob(self, parsed):
        neg, atoms = parsed
        k = key(parsed)
        if k not in self._cache:
            v = np.ones_like(self.p)
            for a in atoms:
                v = v * self._atom(a)
            q = float((self.p * v).sum())
            self._cache[k] = 1 - q if neg else q
        return self._cache[k]

    def basic(self):
        return {"1": self.prob((False, [("res", "", {"1"})])), "X": self.prob((False, [("res", "", {"X"})])),
                "Over": self.prob((False, [("tot", "", "T", 3, 99)]))}


def fit(p1, px, p_over):
    """No-vig P(home), P(draw), P(over 2.5) -> Match whose model reproduces them (least squares)."""
    from scipy.optimize import minimize
    target = np.array([p1, px, p_over])

    def loss(v):
        lh, la, rho = v
        if lh <= 0.05 or la <= 0.05 or abs(rho) > 0.3:
            return 1e3
        b = Match(lh, la, rho).basic()
        return float(((np.array([b["1"], b["X"], b["Over"]]) - target) ** 2).sum())

    total = max(0.6, -math.log(max(1e-6, 1 - p_over)) * 1.3)
    share = 0.5 + (p1 - (1 - p1 - px)) * 0.6
    res = minimize(loss, [total * share, total * (1 - share), 0.0], method="Nelder-Mead",
                   options={"xatol": 1e-4, "fatol": 1e-9, "maxiter": 400})
    m = Match(*res.x)
    m.fit_error = math.sqrt(res.fun / 3)
    return m
