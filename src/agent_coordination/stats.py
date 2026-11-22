"""Paired statistics with the instance as the unit of analysis.

A bargaining study plays every cell on the same instances, so every comparison is paired by instance. Where a
strategy is adopted alone in either seat, the two seat-wise differences of one instance share the same baseline
game and are not independent: they are averaged into one difference per instance before any test (pooling them
as separate pairs would count each instance twice). Tests: the exact two-sided sign test, the Wilcoxon
signed-rank test, a percentile bootstrap interval for the mean difference (resampling instances), the
standardized effect d_z, and Holm's correction for a family of hypotheses. No dependencies beyond the standard
library; rows are the dictionaries of a study's rows.csv.
"""
import csv
import math
import random
import statistics
from pathlib import Path

STYLES = ("competing", "collaborating", "compromising", "accommodating", "avoiding")


def sign_test(d):
    wins, losses = sum(v > 0 for v in d), sum(v < 0 for v in d)
    n, k = wins + losses, min(wins, losses)
    p = 1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)
    return {"wins": wins, "losses": losses, "p": p}


def wilcoxon(d):
    """Two-sided signed-rank test; zeros dropped, average ranks for ties, normal approximation with tie and
    continuity corrections (exact by enumeration when there are at most 20 untied non-zero differences)."""
    x = [v for v in d if v != 0]
    n = len(x)
    if n == 0:
        return {"n": 0, "w_plus": 0.0, "p": 1.0}
    order = sorted(range(n), key=lambda i: abs(x[i]))
    ranks, i, ties = [0.0] * n, 0, []
    while i < n:
        j = i
        while j + 1 < n and abs(x[order[j + 1]]) == abs(x[order[i]]):
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        ties.append(j - i + 1)
        i = j + 1
    w_plus = sum(r for r, v in zip(ranks, x) if v > 0)
    total = n * (n + 1) / 2
    if n <= 20 and all(t == 1 for t in ties):
        counts = [1] + [0] * int(total)  # number of sign assignments giving each rank sum
        for r in range(1, n + 1):
            for s in range(int(total), r - 1, -1):
                counts[s] += counts[s - r]
        lo = min(w_plus, total - w_plus)
        p = min(1.0, 2 * sum(counts[: int(lo) + 1]) / 2 ** n)
        return {"n": n, "w_plus": w_plus, "p": p}
    mean = total / 2
    var = n * (n + 1) * (2 * n + 1) / 24 - sum(t ** 3 - t for t in ties) / 48
    if var <= 0:
        return {"n": n, "w_plus": w_plus, "p": 1.0}
    z = (abs(w_plus - mean) - 0.5) / math.sqrt(var)
    return {"n": n, "w_plus": w_plus, "p": min(1.0, math.erfc(max(z, 0.0) / math.sqrt(2)))}


def bootstrap_ci(d, resamples=10000, level=0.95, seed=20261122):
    """Percentile interval for the mean of d, resampling the instances."""
    if len(d) < 2:
        return [None, None]
    rng, n = random.Random(seed), len(d)
    means = sorted(sum(d[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    a = (1 - level) / 2
    return [means[int(a * resamples)], means[min(resamples - 1, int((1 - a) * resamples))]]


def summary(d, resamples=10000):
    """Everything reported for one paired comparison: d holds one difference per instance."""
    d = [v for v in d if v is not None]
    if not d:
        return None
    sd = statistics.stdev(d) if len(d) > 1 else 0.0
    mean = statistics.mean(d)
    s, w = sign_test(d), wilcoxon(d)
    return {"n": len(d), "mean": mean, "sd": sd, "dz": mean / sd if sd else None, "ci95": bootstrap_ci(d, resamples),
            "wins": s["wins"], "losses": s["losses"], "p_sign": s["p"], "p_wilcoxon": w["p"]}


def holm(pvalues):
    """Holm-adjusted p-values, in the order given."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    out, running = [0.0] * m, 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        out[i] = running
    return out


# ---------------------------------------------------------------- per-instance differences from a study's rows

NUMERIC = ("payoff_p1", "payoff_p2", "joint", "max_joint", "joint_gap", "p1_tokens", "p2_tokens", "moves")


def load_rows(path):
    """A study's rows.csv as {(cell, instance): row}, errors left out, numbers parsed."""
    out = {}
    with open(Path(path)) as handle:
        for r in csv.DictReader(handle):
            if r["error"] == "True":
                continue
            for k in NUMERIC:
                r[k] = float(r[k]) if r.get(k) not in (None, "") else None
            r["agreement"], r["on_frontier"] = r["agreement"] == "True", r["on_frontier"] == "True"
            out[(r["cell"], int(r["instance"]))] = r
    return out


def instances_of(R, cell="neutral-neutral"):
    return sorted(i for (c, i) in R if c == cell)


def other(seat):
    return "p2" if seat == "p1" else "p1"


def seat_cells(strategy, partner):
    """(cell, seat of the strategy) for the strategy facing the partner strategy in either seat."""
    if strategy == partner:
        return [(f"{strategy}-{strategy}", "p1"), (f"{strategy}-{strategy}", "p2")]
    return [(f"{strategy}-{partner}", "p1"), (f"{partner}-{strategy}", "p2")]


def joint_payoff(R, i, strategy, partner):
    vals = [R[(cell, i)]["joint"] for cell, _ in seat_cells(strategy, partner) if (cell, i) in R]
    return statistics.mean(vals) if vals else None


def matched(R, strategy, partner, base, scale=None):
    """Per-instance differences between `strategy` and `base`, each facing `partner`, matched by seat: for every
    seat in which `strategy` was played, its game minus the game with `base` in the same seat, then averaged over
    the seats of the instance. Returns the differences in the adopter's own payoff, its partner's payoff and the
    joint payoff; own and partner in percent of the seat's maximum when scale = {"p1": max, "p2": max} is given."""
    out = {"own": [], "partner": [], "joint": []}
    for i in instances_of(R):
        d = {"own": [], "partner": [], "joint": []}
        for cell, seat in seat_cells(strategy, partner):
            base_cell = f"{base}-{partner}" if seat == "p1" else f"{partner}-{base}"
            r, b = R.get((cell, i)), R.get((base_cell, i))
            if r is None or b is None:
                continue
            k_own = 100.0 / scale[seat] if scale else 1.0
            k_par = 100.0 / scale[other(seat)] if scale else 1.0
            d["own"].append((r[f"payoff_{seat}"] - b[f"payoff_{seat}"]) * k_own)
            d["partner"].append((r[f"payoff_{other(seat)}"] - b[f"payoff_{other(seat)}"]) * k_par)
            d["joint"].append(r["joint"] - b["joint"])
        for k in out:
            if d[k]:
                out[k].append(statistics.mean(d[k]))
    return out


def alone(R, strategy, scale=None, partner="neutral"):
    """Adopted alone against `partner` (the default agent unless given), against `partner` in the same seat."""
    return matched(R, strategy, partner, partner, scale)


def pair(R, strategy, base="neutral"):
    """Both sides use the strategy: per-instance joint payoff minus the `base` pair's."""
    out = []
    for i in instances_of(R):
        a, b = joint_payoff(R, i, strategy, strategy), joint_payoff(R, i, base, base)
        if a is not None and b is not None:
            out.append(a - b)
    return out


def reply(R, strategy, to="competing", base="neutral", scale=None):
    """Own payoff of `strategy` against a `to` partner minus the `base` reply's in the same seat, per instance."""
    return matched(R, strategy, to, base, scale)["own"]


def contrast(R, strategy, other_strategy, partner="neutral", scale=None):
    """Own payoff of `strategy` minus `other_strategy`, each adopted against `partner`, per instance."""
    return matched(R, strategy, partner, other_strategy, scale)["own"]
