"""Incident-to-test synthesis: turn detected symptoms into an executable failing test.

The generated test replays the captured request journal (traffic + price updates, with the
same RNG seed) against the code under test and asserts that each detected symptom is gone.
It is red on the broken build and must be green after a fix, so "fixed" is tied to the
actual incident rather than to whatever the existing suite happens to cover.
"""
from pathlib import Path

_TELEMETRY_SRC = (Path(__file__).with_name("telemetry.py")).read_text()

_HEADER = '''
import json
import pathlib
import unittest

from shop.workload import run_journal

_JOURNAL = json.loads(pathlib.Path(__file__).with_name("journal.json").read_text())
_SHOP, _RESULTS = run_journal(_JOURNAL, %(seed)d)
S = summarize(_SHOP.tel.export())
N = S["n_requests"]


class IncidentRepro(unittest.TestCase):
'''

_CHECKS = {
    "log_signature": (
        "        count = S['log_counts'].get(%(key)r, 0)\n"
        "        rate = count / N\n"
        "        self.assertFalse(count >= 3 and rate > 3 * %(base)r + 0.01,\n"
        "                         'symptom still present: %%s x%%d' %% (%(key)r, count))\n"),
    "error_rate": (
        "        errs = S['error_count'].get(%(svc)r, 0)\n"
        "        rate = errs / max(S['span_count'].get(%(svc)r, 1), 1)\n"
        "        self.assertFalse(errs >= 3 and rate - %(base)r > 0.03,\n"
        "                         'error rate still high for %(svc)s: %%.3f' %% rate)\n"),
    "latency_p95": (
        "        obs = S['p95_latency'].get(%(metric)r, 0.0)\n"
        "        calls = S['span_calls'].get(%(metric)r, 0)\n"
        "        self.assertFalse(calls >= 5 and obs > 1.5 * %(base)r and obs - %(base)r > 0.03,\n"
        "                         'p95 still high for %(metric)s: %%.3f' %% obs)\n"),
    "calls_per_request": (
        "        obs = S['calls_per_request'].get(%(svc)r, 0.0)\n"
        "        self.assertFalse(obs > 1.4 * %(base)r and obs - %(base)r > 0.3,\n"
        "                         'call amplification still present for %(svc)s: %%.2f' %% obs)\n"),
    "self_time": (
        "        obs = S['self_time_per_request'].get(%(svc)r, 0.0)\n"
        "        self.assertFalse(obs - %(base)r > 0.05 and obs > 1.5 * %(base)r,\n"
        "                         'self time still high for %(svc)s: %%.3f' %% obs)\n"),
}


def synthesize_repro_test(anomalies, seed):
    src = [_TELEMETRY_SRC, _HEADER % {"seed": seed}]
    for i, a in enumerate(anomalies):
        key = "%s|%s" % (a.service, a.metric.replace(":", "|")) if a.kind == "log_signature" else ""
        body = _CHECKS[a.kind] % {"key": key, "svc": a.service, "metric": a.metric,
                                  "base": a.baseline}
        src.append("    def test_%02d_%s_%s(self):\n%s\n" % (
            i, a.kind, "".join(c if c.isalnum() else "_" for c in a.service + a.metric), body))
    return "\n".join(src)
