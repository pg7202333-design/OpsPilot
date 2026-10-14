"""End-to-end pipeline: detect -> investigate -> explain -> reproduce -> repair -> verify."""
import time
import re
import json
from dataclasses import dataclass, field

from opspilot.bench.incidents import (BASELINE_SEEDS, ORACLE_SEEDS, public_deploys)
from opspilot.detect import detect, symptom_text
from opspilot.investigate import investigate, render_report
from opspilot.llm import LLMError, cost_usd
from opspilot.memory import IncidentMemory, PastIncident
from opspilot.repair import (LLMPatcher, PatchParseError, RollbackPatcher,
                             patch_policy_rejection, rule_based_candidates)
from opspilot.reproduce import synthesize_repro_test
from opspilot.sandbox import WRITE_ALLOWLIST, Sandbox, SandboxError
from opspilot.telemetry import pool, summarize
from opspilot.verify import verify
from shop.workload import make_journal, run_journal


@dataclass
class PipelineConfig:
    name: str = "opspilot"
    use_memory: bool = True
    weights: dict = field(default_factory=dict)
    verify: bool = True
    use_repro: bool = True
    top_k: int = 3
    llm: object = None          # client with .complete() and .usage; enables the LLM fallback
    llm_attempts: int = 2
    price_in: float = None      # USD per million input tokens (optional, for cost reporting)
    price_out: float = None


@dataclass
class IncidentResult:
    incident_id: str
    family: str
    seed: int
    latent: bool = False
    has_culprit: bool = True
    detected: bool = False
    ranked_ids: list = field(default_factory=list)
    confidence: float = 0.0
    top1_correct: bool = False
    top3_correct: bool = False
    reproduced: bool = False
    shipped: bool = False
    shipped_id: str = ""
    patch_source: str = ""      # "rollback" | "llm" | ""
    verified: bool = False
    oracle_fixed: bool = False
    attempts: int = 0
    llm_attempts: int = 0
    latency_s: float = 0.0
    tokens: int = 0
    cost_usd: float = 0.0
    llm_error: str = ""
    report: str = ""


class Context:
    """Shared, cached reference data: healthy baseline, oracle outputs, past observations."""

    def __init__(self):
        self.base = pool([summarize(run_journal(make_journal(s), s)[0].tel.export())
                          for s in BASELINE_SEEDS])
        self.healthy_oracle = {s: run_journal(make_journal(s), s)[1] for s in ORACLE_SEEDS}
        self._obs = {}

    def observe(self, inc):
        """Replay the incident traffic against the broken build; cached per incident."""
        if inc.id not in self._obs:
            with Sandbox(edits=inc.edits) as sb:
                tel, _ = sb.replay(make_journal(inc.journal_seed), inc.journal_seed)
            obs = summarize(tel)
            self._obs[inc.id] = (obs, detect(self.base, obs))
        return self._obs[inc.id]

    def memory_for(self, inc, bench):
        """Incidents resolved earlier in the stream (strictly lower seed round)."""
        mem = IncidentMemory()
        for past in bench:
            if past.seed < inc.seed:
                _, anomalies = self.observe(past)
                c = past.culprit
                if c is not None:
                    cause, fix = c["message"] + " " + " ".join(c["diff"]), "revert %s" % c["id"]
                else:  # latent incident: the postmortem names the faulty code, not a deploy
                    cause, fix = past.summary, "patch: " + past.summary
                mem.add(PastIncident(past.id, symptom_text(anomalies), cause, fix))
        return mem

    def sources_for(self, inc):
        """Current contents of the editable service files on the broken build."""
        with Sandbox(edits=inc.edits) as sb:
            return {f: sb.read(f) for f in WRITE_ALLOWLIST}

    def oracle_ok(self, sb):
        """Hidden ground truth. The build must (a) return exactly the healthy build's results
        and (b) show no telemetry anomaly vs the baseline, on traffic the agent never sees.
        (b) matters for faults that only hurt latency or call counts, where results are
        identical to the healthy build even while the incident is ongoing."""
        try:
            for seed in ORACLE_SEEDS:
                tel, results = sb.replay(make_journal(seed), seed)
                if results != self.healthy_oracle[seed] or detect(self.base, summarize(tel)):
                    return False
        except SandboxError:
            return False
        return True

    def oracle_fixed(self, inc, patch_edits):
        with Sandbox(edits=inc.edits) as sb:
            try:
                sb.apply_patch(patch_edits)
            except SandboxError:
                return False
            return self.oracle_ok(sb)


def _is_expected_test_failure(result):
    """Return True only when unittest reports assertion failures, not test errors."""
    if result.ok or result.timed_out:
        return False

    output = (result.stdout or "") + "\n" + (result.stderr or "")

    # Confirm unittest actually ran tests.
    if not re.search(r"\bRan \d+ tests?\b", output):
        return False

    # Find unittest's final failure summary.
    match = re.search(r"\bFAILED \(([^)]*)\)", output)
    if not match:
        return False

    counts = dict(re.findall(r"(failures|errors)=(\d+)", match.group(1)))

    # A genuine red test has assertion failures and no test errors.
    failures = int(counts.get("failures", 0))
    errors = int(counts.get("errors", 0))

    return failures > 0 and errors == 0

def _feedback(v):
    if "error" in v.detail:
        return v.detail["error"]
    bits = []
    if not v.repro_passed:
        bits.append("the incident repro test still fails: %s" % v.detail.get("repro_output", ""))
    if not v.regression_passed:
        bits.append("the existing regression suite fails: %s" % v.detail.get("regression_output", ""))
    if not v.heldout_clean:
        bits.append("fresh traffic still shows anomalies: %s" % (
            v.detail.get("heldout_anomalies") or v.detail.get("heldout_error", "")))
    return "; ".join(bits)[:1200]


def run_incident(inc, ctx, cfg, bench=(), patcher=None):
    patcher = patcher or RollbackPatcher()
    start = time.perf_counter()
    res = IncidentResult(inc.id, inc.family, inc.seed, latent=inc.latent,
                         has_culprit=inc.has_culprit)

    obs, anomalies = ctx.observe(inc)
    res.detected = bool(anomalies)
    if not anomalies:
        res.latency_s = time.perf_counter() - start
        return res

    memory = ctx.memory_for(inc, bench) if cfg.use_memory else None
    hyps = investigate(anomalies, obs, public_deploys(inc), inc.ts, memory=memory,
                       weights=cfg.weights, top_k=cfg.top_k)
    res.ranked_ids = [h.deploy_id for h in hyps]
    if hyps:
        res.confidence = hyps[0].confidence
        res.top1_correct = inc.has_culprit and hyps[0].deploy_id == inc.culprit_id
        res.top3_correct = inc.has_culprit and inc.culprit_id in res.ranked_ids
    res.report = render_report(inc.id, anomalies, hyps)

    journal = make_journal(inc.journal_seed)
    repro_src = synthesize_repro_test(anomalies, inc.journal_seed)

    with Sandbox(edits=inc.edits) as sb:
        sb.install_repro(journal, repro_src)
        repro_result = sb.run_tests("repro")
        res.reproduced = _is_expected_test_failure(repro_result)


    patch = None
    by_id = {d["id"]: d for d in public_deploys(inc)}
    for h in hyps:
        edits = patcher.propose(by_id[h.deploy_id], res.report)
        res.attempts += 1
        if not cfg.verify:
            res.shipped, res.shipped_id, patch = True, h.deploy_id, edits
            res.patch_source = patcher.name
            break
        v = verify(inc.edits, edits, repro_src, journal, ctx.base, use_repro=cfg.use_repro)
        if v.verified:
            res.shipped, res.shipped_id, res.verified, patch = True, h.deploy_id, True, edits
            res.patch_source = patcher.name
            break

    if patch is None and cfg.verify and cfg.llm is not None:
        patch = _llm_repair(inc, ctx, cfg, res, repro_src, journal)

    # Model output is untrusted. If it cannot produce a verified repair, try conservative
    # source-pattern candidates and accept one only if the same independent verifier passes.
    if patch is None or not res.shipped:
        sources = ctx.sources_for(inc)
        for candidate in rule_based_candidates(res.report, sources):
            res.attempts += 1
            v = verify(inc.edits, [candidate], repro_src, journal, ctx.base, use_repro=cfg.use_repro)
            if v.verified:
                patch = [candidate]
                res.shipped, res.verified = True, True
                res.shipped_id, res.patch_source = "verified-rule", "rules"
                break

    if res.shipped:
        res.oracle_fixed = ctx.oracle_fixed(inc, patch)
    res.latency_s = time.perf_counter() - start
    return res


def _llm_repair(inc, ctx, cfg, res, repro_src, journal):
    """Fallback when no rollback verified: ask the model, feed back why a patch was rejected."""
    llm = LLMPatcher(cfg.llm)
    sources, deploys = ctx.sources_for(inc), public_deploys(inc)
    before = cfg.llm.usage.snapshot()
    feedback = None
    patch = None
    for _ in range(cfg.llm_attempts):
        res.attempts += 1
        res.llm_attempts += 1
        try:
            edits = llm.propose(res.report, sources, deploys, feedback)
            print("AI PATCH EXPLANATION:", llm.last_explanation)
            print("AI PROPOSED EDITS:", edits)
        except PatchParseError as exc:
            feedback = "Your response could not be used: %s" % exc
            continue
        except LLMError as exc:
            res.llm_error = str(exc)
            break
        policy_reason = patch_policy_rejection(res.report, edits)
        if policy_reason:
            feedback = ("The patch was rejected by safety policy.\n" + policy_reason +
                        "\nPrevious rejected patch (do not repeat it):\n" +
                        json.dumps(edits, ensure_ascii=False) +
                        "\nMake a minimal fix in the upstream value-producing code.")[:2200]
            print("PATCH POLICY REJECTION:", feedback)
            continue
        v = verify(inc.edits, edits, repro_src, journal, ctx.base, use_repro=cfg.use_repro)
        if v.verified:
            res.shipped, res.verified, patch = True, True, edits
            res.shipped_id, res.patch_source = "llm-patch", "llm"
            break
        feedback_parts = ["The patch was rejected by the verifier."]
        feedback_parts.append("Previous rejected patch (do not repeat it):\n" +
                              json.dumps(edits, ensure_ascii=False))
        feedback_parts.append("Verifier evidence: " + _feedback(v))
        feedback_parts.append(
            "Make a different, minimal edit that fixes the underlying behavior. "
            "Preserve existing function signatures, return types, and regression behavior."
        )
        feedback = "\n\n".join(feedback_parts)[:2200]
        print("VERIFIER REJECTION:", feedback)
    after = cfg.llm.usage.snapshot()
    d_in, d_out = after[0] - before[0], after[1] - before[1]
    res.tokens = d_in + d_out
    res.cost_usd = cost_usd(d_in, d_out, cfg.price_in, cfg.price_out) or 0.0
    return patch
