"""Stage 1 - Detect: compare an incident window of telemetry against a healthy baseline."""
import math
from dataclasses import dataclass, field


@dataclass
class Anomaly:
    kind: str          # log_signature | error_rate | latency_p95 | calls_per_request | self_time
    service: str
    metric: str
    baseline: float
    observed: float
    severity: float    # 0..1
    detail: dict = field(default_factory=dict)

    def describe(self):
        return "%s %s [%s]: baseline=%.3f observed=%.3f (severity %.2f)" % (
            self.kind, self.service, self.metric, self.baseline, self.observed, self.severity)


def _sev(obs, base, scale):
    ratio = (obs - base) / (abs(base) + scale)
    return min(1.0, math.log1p(max(ratio, 0.0)) / math.log1p(20.0))


def detect(base, obs):
    out = []
    n_o, n_b = obs["n_requests"], base["n_requests"]

    for key, count in obs["log_counts"].items():
        service, level, msg = key.split("|")
        b_rate = base["log_counts"].get(key, 0) / n_b
        o_rate = count / n_o
        if count >= 3 and o_rate > 3 * b_rate + 0.01:
            out.append(Anomaly("log_signature", service, "%s:%s" % (level, msg), b_rate, o_rate,
                               _sev(o_rate, b_rate, 0.02),
                               {"count": count, "sample": obs["log_samples"].get(key, {})}))

    for service, spans in obs["span_count"].items():
        errs = obs["error_count"].get(service, 0)
        o_rate = errs / spans
        b_rate = base["error_count"].get(service, 0) / max(base["span_count"].get(service, 1), 1)
        if errs >= 3 and o_rate - b_rate > 0.03:
            out.append(Anomaly("error_rate", service, "error_rate", b_rate, o_rate,
                               _sev(o_rate, b_rate, 0.02), {"errors": errs}))

    for key, o_p95 in obs["p95_latency"].items():
        b_p95 = base["p95_latency"].get(key)
        if b_p95 is None or obs["span_calls"].get(key, 0) < 5:
            continue
        if o_p95 > 1.5 * b_p95 and o_p95 - b_p95 > 0.03:
            out.append(Anomaly("latency_p95", key.split(".")[0], key, b_p95, o_p95,
                               _sev(o_p95, b_p95, 0.05)))

    for service, o_cpr in obs["calls_per_request"].items():
        b_cpr = base["calls_per_request"].get(service, 0.0)
        if o_cpr > 1.4 * b_cpr and o_cpr - b_cpr > 0.3:
            names = [n for n, v in obs["name_calls_per_request"][service].items()
                     if v > 1.4 * base["name_calls_per_request"].get(service, {}).get(n, 0.0)]
            out.append(Anomaly("calls_per_request", service, "calls_per_request", b_cpr, o_cpr,
                               _sev(o_cpr, b_cpr, 0.5), {"span_names": sorted(names)}))

    for service, o_st in obs["self_time_per_request"].items():
        b_st = base["self_time_per_request"].get(service, 0.0)
        if o_st - b_st > 0.05 and o_st > 1.5 * b_st:
            out.append(Anomaly("self_time", service, "self_time_per_request", b_st, o_st,
                               _sev(o_st, b_st, 0.05)))

    out.sort(key=lambda a: -a.severity)
    return out


def symptom_text(anomalies):
    """Compact textual description of an incident, used for memory retrieval."""
    parts = []
    for a in anomalies:
        parts.append("%s %s %s" % (a.kind, a.service, a.metric))
        parts.extend(a.detail.get("span_names", []))
        for k, v in a.detail.get("sample", {}).items():
            if k not in ("service", "level"):
                parts.append("%s %s" % (k, v))
    return " ".join(parts)
