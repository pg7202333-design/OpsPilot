"""Telemetry summarisation. Stdlib-only and self-contained: this file is inlined verbatim
into synthesized repro tests, so it must not import anything from opspilot."""
import math
from collections import defaultdict


def p95(xs):
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]


def summarize(tel):
    """tel: {'logs': [...], 'spans': [...]} as exported by shop.telemetry.Telemetry."""
    spans, logs = tel["spans"], tel["logs"]
    by_id = {s["span_id"]: s for s in spans}
    child_dur = defaultdict(float)
    errored_parent = set()
    edges = defaultdict(int)
    for s in spans:
        p = s["parent_id"]
        if p is not None:
            child_dur[p] += s["end"] - s["start"]
            edges["%s->%s" % (by_id[p]["service"], s["service"])] += 1
            if s["status"] == "error":
                errored_parent.add(p)
    n = sum(1 for s in spans if s["parent_id"] is None) or 1

    span_count, error_count = defaultdict(int), defaultdict(int)
    self_time, blame = defaultdict(float), defaultdict(int)
    durations, name_counts = defaultdict(list), defaultdict(lambda: defaultdict(int))
    for s in spans:
        svc, dur = s["service"], s["end"] - s["start"]
        span_count[svc] += 1
        name_counts[svc][s["name"]] += 1
        durations["%s.%s" % (svc, s["name"])].append(dur)
        self_time[svc] += dur - child_dur[s["span_id"]]
        if s["status"] == "error":
            error_count[svc] += 1
            if s["span_id"] not in errored_parent:
                blame[svc] += 1

    log_counts, log_samples = defaultdict(int), {}
    for rec in logs:
        if rec["level"] in ("WARN", "ERROR"):
            key = "%s|%s|%s" % (rec["service"], rec["level"], rec["msg"])
            log_counts[key] += 1
            log_samples.setdefault(key, {k: v for k, v in rec.items()
                                         if k not in ("ts", "trace_id")})
    return _package(n, span_count, error_count, durations, name_counts, self_time,
                    blame, log_counts, log_samples, edges)


def _package(n, span_count, error_count, durations, name_counts, self_time,
             blame, log_counts, log_samples, edges):
    return {
        "n_requests": n,
        "span_count": dict(span_count),
        "error_count": dict(error_count),
        "p95_latency": {k: p95(v) for k, v in durations.items()},
        "span_calls": {k: len(v) for k, v in durations.items()},
        "calls_per_request": {k: v / n for k, v in span_count.items()},
        "name_calls_per_request": {s: {k: c / n for k, c in d.items()}
                                   for s, d in name_counts.items()},
        "self_time_per_request": {k: v / n for k, v in self_time.items()},
        "blame_per_request": {k: v / n for k, v in blame.items()},
        "log_counts": dict(log_counts),
        "log_samples": log_samples,
        "call_edges": dict(edges),
    }


def pool(summaries):
    """Combine several healthy-run summaries into one lower-variance baseline."""
    k = len(summaries)

    def total(field):
        out = defaultdict(float)
        for s in summaries:
            for key, v in s[field].items():
                out[key] += v
        return dict(out)

    def mean(field):
        return {key: v / k for key, v in total(field).items()}

    names = defaultdict(lambda: defaultdict(float))
    for s in summaries:
        for svc, d in s["name_calls_per_request"].items():
            for name, v in d.items():
                names[svc][name] += v / k
    samples = {}
    for s in summaries:
        for key, v in s["log_samples"].items():
            samples.setdefault(key, v)
    return {
        "n_requests": sum(s["n_requests"] for s in summaries),
        "span_count": total("span_count"),
        "error_count": total("error_count"),
        "p95_latency": mean("p95_latency"),
        "span_calls": total("span_calls"),
        "calls_per_request": mean("calls_per_request"),
        "name_calls_per_request": {s: dict(d) for s, d in names.items()},
        "self_time_per_request": mean("self_time_per_request"),
        "blame_per_request": mean("blame_per_request"),
        "log_counts": total("log_counts"),
        "log_samples": samples,
        "call_edges": total("call_edges"),
    }
