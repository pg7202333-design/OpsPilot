"""Stage 5 - Verify: a patch is accepted only if it passes all three independent checks.

  1. repro    - the synthesized incident test goes from red to green
  2. regress  - the existing regression suite still passes (no new failures)
  3. held-out - fresh traffic (unseen seeds) produces no anomalies against the baseline
"""
from dataclasses import dataclass, field

from opspilot.bench.incidents import HELDOUT_SEEDS
from opspilot.detect import detect
from opspilot.sandbox import Sandbox, SandboxError
from opspilot.telemetry import summarize
from shop.workload import make_journal


@dataclass
class Verification:
    repro_passed: bool = False
    regression_passed: bool = False
    heldout_clean: bool = False
    verified: bool = False
    detail: dict = field(default_factory=dict)


def verify(code_edits, patch_edits, repro_source, journal, base_summary, use_repro=True):
    v = Verification()
    try:
        sb = Sandbox(edits=code_edits)
    except SandboxError as exc:
        v.detail["error"] = str(exc)
        return v
    with sb:
        try:
            sb.apply_patch(patch_edits)
        except SandboxError as exc:
            v.detail["error"] = "patch rejected: %s" % exc
            return v

        if use_repro:
            sb.install_repro(journal, repro_source)
            res = sb.run_tests("repro")
            v.repro_passed = res.ok
            v.detail["repro_output"] = res.stderr.strip()[-300:]
        else:
            v.repro_passed = True

        res = sb.run_tests("shop_tests")
        v.regression_passed = res.ok
        v.detail["regression_output"] = res.stderr.strip()[-300:]

        clean = True
        for seed in HELDOUT_SEEDS:
            try:
                tel, _ = sb.replay(make_journal(seed), seed)
            except SandboxError as exc:
                clean = False
                v.detail["heldout_error"] = str(exc)
                break
            anomalies = detect(base_summary, summarize(tel))
            if anomalies:
                clean = False
                v.detail["heldout_anomalies"] = [a.describe() for a in anomalies[:3]]
                break
        v.heldout_clean = clean
    v.verified = v.repro_passed and v.regression_passed and v.heldout_clean
    return v
