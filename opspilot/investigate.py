"""Stage 2/3 - Investigate and explain: rank candidate root-cause deploys with evidence.

Each recent deploy is scored on four interpretable features:
  proximity - does the change touch the component the symptoms localize to (or its caller)?
  overlap   - do symptom terms (log codes, span names, field names) appear in the diff?
  recency   - how soon before the incident was it shipped?
  memory    - does it resemble the cause of a similar, previously resolved incident?
Scores are turned into confidences with a softmax so they can be calibrated and used for
risk-coverage analysis (see eval/).
"""
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from opspilot.memory import tokens, token_list

KIND_WEIGHT = {"log_signature": 1.0, "self_time": 1.0, "calls_per_request": 0.8,
               "error_rate": 0.7, "latency_p95": 0.3}
DEFAULT_WEIGHTS = {"proximity": 1.0, "overlap": 1.5, "recency": 0.4, "memory": 1.0}
TEMPERATURE = 1.0  # fit on incident-stream rounds 1-2 by minimising ECE (see eval/run_bench.py)
CALLER_DISCOUNT = 0.6


@dataclass
class Hypothesis:
    deploy_id: str
    file: str
    message: str
    score: float
    confidence: float
    evidence: list = field(default_factory=list)
    features: dict = field(default_factory=dict)


def localization(anomalies):
    """service -> weight in [0, 1]: how strongly the symptoms point at that service."""
    raw = defaultdict(float)
    for a in anomalies:
        raw[a.service] += a.severity * KIND_WEIGHT.get(a.kind, 0.5)
    top = max(raw.values(), default=0.0)
    return {s: v / top for s, v in raw.items()} if top else {}


def symptom_weights(anomalies):
    w = Counter()
    for a in anomalies:
        base = a.severity * KIND_WEIGHT.get(a.kind, 0.5)
        words = [a.service, a.metric] + list(a.detail.get("span_names", []))
        for k, v in a.detail.get("sample", {}).items():
            words.append("%s %s" % (k, v))
        for t in tokens(" ".join(words)):
            w[t] = max(w[t], base)
    return w


def _callers(obs):
    callers = defaultdict(set)
    for edge in obs.get("call_edges", {}):
        parent, child = edge.split("->")
        callers[child].add(parent)
    return callers


def investigate(anomalies, obs, deploys, incident_ts, memory=None, weights=None,
                window_h=72.0, top_k=3, temperature=TEMPERATURE):
    w = dict(DEFAULT_WEIGHTS)
    w.update(weights or {})
    cands = [d for d in deploys
             if d["ts"] <= incident_ts and (incident_ts - d["ts"]) / 3600.0 <= window_h]
    if not cands or not anomalies:
        return []

    loc = localization(anomalies)
    sym = symptom_weights(anomalies)
    callers = _callers(obs)
    dtoks = {d["id"]: tokens(d["message"] + " " + " ".join(d["diff"])) for d in cands}

    df = Counter(t for ts in dtoks.values() for t in ts)
    idf = {t: math.log((len(cands) + 1) / (1 + df[t])) + 1.0 for t in df}

    def overlap_of(d):
        return sum(sym[t] * idf.get(t, 1.0) for t in sym if t in dtoks[d["id"]])

    raw_overlap = {d["id"]: overlap_of(d) for d in cands}
    max_overlap = max(raw_overlap.values()) or 1.0

    hits = memory.query(_symptom_query(anomalies), k=3) if memory is not None else []
    raw_memory = {}
    for d in cands:
        best = 0.0
        for sim, entry in hits:
            r = tokens(entry.root_cause)
            u = dtoks[d["id"]] | r
            best = max(best, sim * (len(dtoks[d["id"]] & r) / len(u) if u else 0.0))
        raw_memory[d["id"]] = best
    max_memory = max(raw_memory.values()) or 1.0

    scored = []
    for d in cands:
        stem = d["file"].split("/")[-1].rsplit(".", 1)[0]
        prox, prox_why = 0.0, ""
        for svc, lw in loc.items():
            if stem == svc or svc in dtoks[d["id"]]:
                val, why = lw, "touches %s" % svc
            elif stem in callers.get(svc, ()):
                val, why = lw * CALLER_DISCOUNT, "%s is a caller of %s" % (stem, svc)
            else:
                continue
            if val > prox:
                prox, prox_why = val, why
        hours = (incident_ts - d["ts"]) / 3600.0
        feats = {"proximity": prox, "overlap": raw_overlap[d["id"]] / max_overlap,
                 "recency": math.exp(-hours / 12.0), "memory": raw_memory[d["id"]] / max_memory
                 if raw_memory[d["id"]] > 0 else 0.0}
        score = sum(w[k] * feats[k] for k in feats)
        ev = []
        if prox:
            ev.append("proximity %.2f: %s" % (prox, prox_why))
        shared = sorted(t for t in sym if t in dtoks[d["id"]] and sym[t] > 0.2)
        if shared:
            ev.append("overlap %.2f: diff mentions %s" % (feats["overlap"], ", ".join(shared[:6])))
        ev.append("shipped %.1fh before incident (recency %.2f)" % (hours, feats["recency"]))
        if feats["memory"]:
            ev.append("memory %.2f: resembles a previously resolved incident" % feats["memory"])
        scored.append((score, d, feats, ev))

    zs = [s / temperature for s, *_ in scored]
    m = max(zs)
    exps = [math.exp(z - m) for z in zs]
    total = sum(exps)
    ranked = sorted(zip(scored, exps), key=lambda x: -x[0][0])
    return [Hypothesis(d["id"], d["file"], d["message"], round(s, 4), e / total, ev, feats)
            for (s, d, feats, ev), e in ranked[:top_k]]


def _symptom_query(anomalies):
    from opspilot.detect import symptom_text
    return symptom_text(anomalies)


def render_report(incident_id, anomalies, hypotheses):
    lines = ["# Incident report: %s" % incident_id, "", "## Detected symptoms"]
    lines += ["- " + a.describe() for a in anomalies[:8]]
    lines += ["", "## Ranked root-cause hypotheses"]
    for i, h in enumerate(hypotheses, 1):
        lines.append("%d. **%s** (%s) - confidence %.0f%%: %s" % (
            i, h.deploy_id, h.file, 100 * h.confidence, h.message))
        lines += ["   - " + e for e in h.evidence]
    return "\n".join(lines)
