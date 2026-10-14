"""Evaluation metrics: accuracy, calibration, risk-coverage, bootstrap CIs (stdlib only)."""
import random


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def ece(confs, correct, bins=5):
    """Expected calibration error with equal-width bins."""
    n = len(confs)
    if n == 0:
        return 0.0
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, c in enumerate(confs) if lo <= c < hi or (b == bins - 1 and c == 1.0)]
        if idx:
            total += len(idx) / n * abs(mean(confs[i] for i in idx) - mean(correct[i] for i in idx))
    return total


def reliability(confs, correct, bins=5):
    rows = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, c in enumerate(confs) if lo <= c < hi or (b == bins - 1 and c == 1.0)]
        if idx:
            rows.append({"bin": "%.1f-%.1f" % (lo, hi), "n": len(idx),
                         "mean_conf": round(mean(confs[i] for i in idx), 3),
                         "accuracy": round(mean(correct[i] for i in idx), 3)})
    return rows


def bootstrap_ci(values, n_boot=2000, seed=0):
    values = list(values)
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    means = sorted(mean(rng.choice(values) for _ in values) for _ in range(n_boot))
    return (means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1])


def risk_coverage(confs, correct, thresholds=(0.0, 0.5, 0.7, 0.9, 0.95, 0.99)):
    """For each confidence threshold: coverage (fraction answered) and risk (error rate)."""
    n = len(confs)
    rows = []
    for t in thresholds:
        idx = [i for i, c in enumerate(confs) if c >= t]
        rows.append({"threshold": t, "coverage": round(len(idx) / n, 3) if n else 0.0,
                     "risk": round(1 - mean(correct[i] for i in idx), 3) if idx else None})
    return rows
