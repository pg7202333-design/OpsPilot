import io
import json
import ssl
import sys
import unittest
import urllib.error
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from opspilot.bench.incidents import (BASELINE_SEEDS, FAULTS, ORACLE_SEEDS, build_bench,  # noqa: E402
                                      build_incident, public_deploys)
from opspilot.detect import detect  # noqa: E402
from opspilot.eval.metrics import ece, risk_coverage  # noqa: E402
from opspilot.pipeline import Context, PipelineConfig, run_incident  # noqa: E402
from opspilot.llm import AnthropicClient, LLMError, ScriptedClient  # noqa: E402
from opspilot.repair import LLMPatcher, PatchParseError, RollbackPatcher, parse_edits  # noqa: E402
from opspilot.reproduce import synthesize_repro_test  # noqa: E402
from opspilot.sandbox import Sandbox, SandboxError  # noqa: E402
from opspilot.telemetry import summarize  # noqa: E402
from opspilot.verify import verify  # noqa: E402
from shop.workload import make_journal, run_journal  # noqa: E402
from opspilot.eval.run_bench import latent_markdown  # noqa: E402
CTX = None


def ctx():
    global CTX
    if CTX is None:
        CTX = Context()
    return CTX


class BenchIntegrity(unittest.TestCase):
    def test_healthy_suite_passes(self):
        with Sandbox() as sb:
            self.assertTrue(sb.run_tests("shop_tests").ok)

    def test_edits_apply_once_and_rollback_restores_source(self):
        for fam in FAULTS:
            inc = build_incident(fam, 1)
            with Sandbox(edits=inc.edits) as broken, Sandbox() as healthy:
                broken.apply_patch(RollbackPatcher().propose(inc.culprit))
                for name in ("inventory", "payments", "config", "catalog", "orders"):
                    f = "shop/%s.py" % name
                    # rollback of the culprit alone must leave only decoy-induced differences
                    if f != inc.culprit["file"]:
                        continue
                    decoy_edits = [d["edits"][0] for d in inc.deploys
                                   if d["kind"] == "decoy" and d["file"] == f]
                    with Sandbox(edits=decoy_edits) as expected:
                        self.assertEqual(broken.read(f), expected.read(f), fam)

    def test_decoys_are_behaviour_preserving(self):
        inc = build_incident("orders_rounding", 2)
        decoys = [d["edits"][0] for d in inc.deploys if d["kind"] == "decoy"]
        with Sandbox(edits=decoys) as sb:
            for seed in ORACLE_SEEDS:
                _, res = sb.replay(make_journal(seed), seed)
                self.assertEqual(res, ctx().healthy_oracle[seed])

    def test_every_family_is_detected(self):
        for fam in FAULTS:
            _, anomalies = ctx().observe(build_incident(fam, 1))
            self.assertTrue(anomalies, fam)


class Detector(unittest.TestCase):
    def test_false_positive_rate_on_healthy_traffic(self):
        fp = 0
        for seed in range(1, 41):
            shop, _ = run_journal(make_journal(seed), seed)
            if detect(ctx().base, summarize(shop.tel.export())):
                fp += 1
        self.assertLessEqual(fp / 40, 0.05)


class SandboxSafety(unittest.TestCase):
    def test_write_outside_allowlist_denied(self):
        with Sandbox() as sb:
            with self.assertRaises(SandboxError):
                sb.apply_patch([{"file": "shop_tests/test_shop.py", "find": "unittest", "replace": "x"}])

    def test_observability_and_driver_are_read_only(self):
        with Sandbox() as sb:
            for f, find in (("shop/telemetry.py", "self.logs.append("),
                            ("shop/workload.py", "def run_journal"),
                            ("shop/app.py", "def handle")):
                with self.assertRaises(SandboxError, msg=f):
                    sb.apply_patch([{"file": f, "find": find, "replace": "pass  # " + find}])

    def test_path_escape_denied(self):
        with Sandbox() as sb:
            with self.assertRaises(SandboxError):
                sb.apply_patch([{"file": "shop/../../etc/hosts", "find": "a", "replace": "b"}])

    def test_inexact_edit_rejected(self):
        with Sandbox() as sb:
            with self.assertRaises(SandboxError):
                sb.apply_patch([{"file": "shop/inventory.py", "find": "self", "replace": "me"}])
            with self.assertRaises(SandboxError):
                sb.apply_patch([{"file": "shop/inventory.py", "find": "nonexistent text", "replace": ""}])

    def test_multiline_replacement_preserves_indentation(self):
        # LLMs sometimes match a statement without its leading spaces. The
        # sandbox may normalize continuation-line indentation, but verification
        # still decides whether the behavior is safe.
        with Sandbox() as sb:
            sb.apply_patch([{
                "file": "shop/inventory.py",
                "find": "self._stock[sku] -= qty",
                "replace": "if qty < 0:\n    return False\nself._stock[sku] -= qty",
            }])
            compile(sb.read("shop/inventory.py"), "shop/inventory.py", "exec")

    def test_timeout_enforced(self):
        with Sandbox() as sb:
            res = sb.run([sys.executable, "-c", "while True: pass"], timeout=1)
            self.assertFalse(res.ok)


class Verification(unittest.TestCase):
    def setUp(self):
        self.inc = build_incident("payments_timeout_config", 1)
        _, self.anomalies = ctx().observe(self.inc)
        self.src = synthesize_repro_test(self.anomalies, self.inc.journal_seed)
        self.journal = make_journal(self.inc.journal_seed)

    def test_repro_is_red_on_broken_build(self):
        with Sandbox(edits=self.inc.edits) as sb:
            sb.install_repro(self.journal, self.src)
            self.assertFalse(sb.run_tests("repro").ok)

    def test_correct_rollback_verifies(self):
        patch = RollbackPatcher().propose(self.inc.culprit)
        v = verify(self.inc.edits, patch, self.src, self.journal, ctx().base)
        self.assertTrue(v.verified, v.detail)

    def test_wrong_patch_is_rejected(self):
        decoy = next(d for d in self.inc.deploys if d["kind"] == "decoy")
        patch = RollbackPatcher().propose(decoy)
        v = verify(self.inc.edits, patch, self.src, self.journal, ctx().base)
        self.assertFalse(v.verified)
        self.assertFalse(v.repro_passed)

    def test_regression_breaking_patch_is_rejected(self):
        patch = RollbackPatcher().propose(self.inc.culprit) + [
            {"file": "shop/ledger.py", "find": "self.tel.advance(0.002)",
             "replace": "raise RuntimeError('boom')"}]
        v = verify(self.inc.edits, patch, self.src, self.journal, ctx().base)
        self.assertFalse(v.verified)


class Pipeline(unittest.TestCase):
    def test_end_to_end_ships_a_true_fix(self):
        inc = build_incident("orders_n_plus_one", 1)
        res = run_incident(inc, ctx(), PipelineConfig(), [inc])
        self.assertTrue(res.detected and res.reproduced)
        self.assertTrue(res.shipped and res.verified and res.oracle_fixed)
        self.assertIn(inc.culprit_id, res.ranked_ids)

    def test_agent_never_sees_ground_truth_labels(self):
        for d in public_deploys(build_incident("catalog_stale_cache", 1)):
            self.assertNotIn("kind", d)


class LatentAndOracle(unittest.TestCase):
    def test_healthy_build_passes_oracle(self):
        with Sandbox() as sb:
            self.assertTrue(ctx().oracle_ok(sb))

    def test_noop_patch_is_not_counted_as_fixed(self):
        # latency-only fault: results equal the healthy build's, telemetry does not
        inc = build_incident("orders_n_plus_one", 1)
        self.assertFalse(ctx().oracle_fixed(inc, []))

    def test_latent_incident_properties(self):
        inc = build_incident("catalog_stale_cache", 1, latent=True)
        self.assertTrue(inc.latent and not inc.has_culprit and inc.culprit is None)
        self.assertTrue(all(d["kind"] == "decoy" for d in inc.deploys))
        self.assertTrue(ctx().oracle_fixed(inc, inc.reference_fix))
        for d in inc.deploys:   # rolling back any deploy cannot fix a latent fault
            self.assertFalse(ctx().oracle_fixed(inc, RollbackPatcher().propose(d)))


def _fix(inc):
    return '```json\n%s\n```' % json.dumps({"explanation": "restore the guard",
                                              "edits": inc.reference_fix})


BENIGN = json.dumps({"explanation": "no-op", "edits": [
    {"file": "shop/ledger.py", "find": "        self.tel = tel\n",
     "replace": "        self.tel = tel\n        self.unused = 0\n"}]})


def run_llm(inc, responses, bench=None, attempts=2):
    client = ScriptedClient(responses)
    cfg = PipelineConfig(llm=client, llm_attempts=attempts, price_in=3.0, price_out=15.0)
    return run_incident(inc, ctx(), cfg, bench or [inc]), client


class LLMRepair(unittest.TestCase):
    def setUp(self):
        self.inc = build_incident("inventory_oversell", 1, latent=True)

    def test_llm_fixes_latent_incident_after_rollbacks_fail(self):
        res, client = run_llm(self.inc, [_fix(self.inc)])
        self.assertTrue(res.shipped and res.verified and res.oracle_fixed)
        self.assertEqual(res.patch_source, "llm")
        self.assertEqual(res.llm_attempts, 1)
        self.assertEqual(res.attempts, 3 + 1)            # 3 rollbacks tried first, then the model
        self.assertGreater(res.tokens, 0)
        self.assertGreater(res.cost_usd, 0)

    def test_rejected_patch_feeds_back_and_second_attempt_succeeds(self):
        res, client = run_llm(self.inc, [BENIGN, _fix(self.inc)])
        self.assertTrue(res.oracle_fixed)
        self.assertEqual(res.llm_attempts, 2)
        self.assertIn("rejected", client.prompts[1][1])
        self.assertNotIn("rejected", client.prompts[0][1])

    def test_rejected_patch_is_included_in_feedback(self):
        res, client = run_llm(self.inc, [BENIGN, _fix(self.inc)])
        self.assertTrue(res.oracle_fixed)
        second_prompt = client.prompts[1][1]
        self.assertIn("Previous rejected patch", second_prompt)
        self.assertIn("self.unused = 0", second_prompt)
        self.assertIn("Preserve existing function signatures", second_prompt)

    def test_prompt_orders_files_using_observed_symptoms(self):
        sources = {
            "shop/payments.py": "class Payments: pass\n",
            "shop/catalog.py": "class Catalog: pass\n",
            "shop/orders.py": "class Orders: pass\n",
            "shop/ledger.py": "class Ledger: pass\n",
        }
        report = ("# Incident report: sample\n\n## Detected symptoms\n"
                  "- calls_per_request catalog: observed=2.5\n\n"
                  "## Ranked root-cause hypotheses\n"
                  "1. shop/payments.py - misleading decoy mentions payments and gateway")
        prompt = LLMPatcher.build_prompt(report, sources, [])
        self.assertLess(prompt.index("### shop/orders.py"), prompt.index("### shop/catalog.py"))
        self.assertLess(prompt.index("### shop/catalog.py"), prompt.index("### shop/payments.py"))
        self.assertIn("not proof of root cause", prompt)
        self.assertNotIn("misleading decoy mentions payments", prompt)
        self.assertIn("ranked root-cause hypotheses are intentionally not included", prompt)

    def test_source_order_ignores_words_in_ranked_hypotheses(self):
        sources = {
            "shop/payments.py": "class Payments: pass\n",
            "shop/catalog.py": "class Catalog: pass\n",
            "shop/orders.py": "class Orders: pass\n",
            "shop/ledger.py": "class Ledger: pass\n",
            "shop/inventory.py": "class Inventory: pass\n",
        }
        report = ("## Detected symptoms\n- log_signature ledger [ERROR:ledger_mismatch]\n"
                  "## Ranked root-cause hypotheses\n"
                  "1. shop/inventory.py - negative_stock inventory payments cache")
        prompt = LLMPatcher.build_prompt(report, sources, [])
        self.assertLess(prompt.index("### shop/orders.py"), prompt.index("### shop/ledger.py"))
        self.assertLess(prompt.index("### shop/catalog.py"), prompt.index("### shop/ledger.py"))
        self.assertNotIn("negative_stock inventory payments cache", prompt)

    def test_verified_rule_fallback_survives_repeated_bad_model_patches(self):
        res, _ = run_llm(self.inc, [BENIGN, BENIGN])
        self.assertTrue(res.shipped and res.verified and res.oracle_fixed)
        self.assertEqual(res.patch_source, "rules")

    def test_malformed_output_recovers(self):
        res, client = run_llm(self.inc, ["I think the bug is in inventory.", _fix(self.inc)])
        self.assertTrue(res.oracle_fixed)
        self.assertIn("could not be used", client.prompts[1][1])

    def test_telemetry_tampering_is_blocked(self):
        hack = json.dumps({"edits": [{"file": "shop/telemetry.py", "find": "self.logs.append(",
                                      "replace": "(lambda *a, **k: None)("}]})
        res, client = run_llm(self.inc, [hack, hack])
        self.assertTrue(res.shipped and res.verified and res.oracle_fixed)
        self.assertEqual(res.patch_source, "rules")
        self.assertIn("write denied", client.prompts[1][1])

    def test_llm_error_is_contained(self):
        def boom(_):
            raise LLMError("HTTP 529: overloaded")
        res, _ = run_llm(self.inc, [boom])
        self.assertTrue(res.shipped and res.verified and res.oracle_fixed)
        self.assertEqual(res.patch_source, "rules")
        self.assertIn("529", res.llm_error)

    def test_llm_not_used_when_rollback_works(self):
        inc = build_incident("orders_rounding", 1)
        res, client = run_llm(inc, [_fix(inc)])
        self.assertEqual(res.patch_source, "rollback")
        self.assertEqual(client.prompts, [])
        self.assertEqual(res.tokens, 0)

    def test_prompt_contains_no_ground_truth(self):
        _, client = run_llm(self.inc, [_fix(self.inc)])
        prompt = client.prompts[0][1].lower()
        self.assertNotIn(self.inc.summary.lower(), prompt)
        self.assertNotIn("decoy", prompt)
        self.assertNotIn("culprit", prompt)
        self.assertNotIn("shop/telemetry.py", prompt)    # only editable files are shown

    def test_latent_memory_path_runs(self):
        bench = build_bench(2, ["orders_rounding"], latent=True)
        res, _ = run_llm(bench[1], [_fix(bench[1])], bench=bench)
        self.assertTrue(res.oracle_fixed)

    def test_parse_edits(self):
        ok, _ = parse_edits('prose {"edits":[{"file":"a","find":"x","replace":"y"}]} more')
        self.assertEqual(ok[0]["find"], "x")
        for bad in ("no json", '{"edits": []}', '{"edits": [{"file": "a", "find": "", "replace": "y"}]}',
                    '{"edits": [{"file": "a", "find": "x"}]}', '{"edits": "x"}', '{bad json}'):
            with self.assertRaises(PatchParseError, msg=bad):
                parse_edits(bad)


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


OK_BODY = {"content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 7, "output_tokens": 3}}


class AnthropicClientTests(unittest.TestCase):
    def client(self):
        return AnthropicClient(api_key="test-key", model="m", retries=2)

    @mock.patch("opspilot.llm.time.sleep")
    def test_success_parses_text_usage_and_sends_headers(self, _sleep):
        seen = {}

        def fake(req, timeout=None, context=None):
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            seen["body"] = json.loads(req.data)
            seen["url"] = req.full_url
            return _Resp(OK_BODY)
        with mock.patch("opspilot.llm.urllib.request.urlopen", fake):
            c = self.client()
            self.assertEqual(c.complete("sys", "user"), "hi")
        self.assertEqual(c.usage.snapshot(), (7, 3))
        self.assertEqual(seen["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(seen["headers"]["x-api-key"], "test-key")
        self.assertEqual(seen["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(seen["body"]["model"], "m")
        self.assertEqual(seen["body"]["system"], "sys")

    @mock.patch("opspilot.llm.time.sleep")
    def test_certificate_error_fails_fast_with_fix_hint(self, _sleep):
        calls = []

        def fake(req, timeout=None, context=None):
            calls.append(1)
            raise urllib.error.URLError(ssl.SSLCertVerificationError("unable to get local issuer certificate"))
        with mock.patch("opspilot.llm.urllib.request.urlopen", fake):
            with self.assertRaises(LLMError) as cm:
                self.client().complete("s", "u")
        self.assertIn("Install Certificates.command", str(cm.exception))
        self.assertEqual(len(calls), 1)            # no pointless retries

    @mock.patch("opspilot.llm.time.sleep")
    def test_auth_error_is_not_retried(self, _sleep):
        calls = []

        def fake(req, timeout=None, context=None):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"invalid x-api-key"))
        with mock.patch("opspilot.llm.urllib.request.urlopen", fake):
            with self.assertRaises(LLMError) as cm:
                self.client().complete("s", "u")
        self.assertIn("HTTP 401", str(cm.exception))
        self.assertEqual(len(calls), 1)

    @mock.patch("opspilot.llm.time.sleep")
    def test_overload_is_retried_then_succeeds(self, _sleep):
        calls = []

        def fake(req, timeout=None, context=None):
            calls.append(1)
            if len(calls) < 3:
                raise urllib.error.HTTPError(req.full_url, 529, "overloaded", {}, io.BytesIO(b"busy"))
            return _Resp(OK_BODY)
        with mock.patch("opspilot.llm.urllib.request.urlopen", fake):
            self.assertEqual(self.client().complete("s", "u"), "hi")
        self.assertEqual(len(calls), 3)

    @mock.patch("opspilot.llm.time.sleep")
    def test_preflight_uses_a_tiny_call_and_surfaces_bad_keys(self, _sleep):
        seen = {}

        def ok(req, timeout=None, context=None):
            seen["max_tokens"] = json.loads(req.data)["max_tokens"]
            return _Resp(OK_BODY)

        def bad(req, timeout=None, context=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"invalid x-api-key"))
        with mock.patch("opspilot.llm.urllib.request.urlopen", ok):
            self.client().preflight()
        self.assertEqual(seen["max_tokens"], 8)
        with mock.patch("opspilot.llm.urllib.request.urlopen", bad):
            with self.assertRaises(LLMError):
                self.client().preflight()

    def test_missing_key_is_a_clear_error(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(LLMError):
                AnthropicClient()


class Metrics(unittest.TestCase):
    def test_ece(self):
        self.assertAlmostEqual(ece([1.0, 1.0], [1.0, 1.0]), 0.0)
        self.assertAlmostEqual(ece([0.9, 0.9], [0.0, 0.0]), 0.9)

    def test_risk_coverage_monotone_coverage(self):
        rows = risk_coverage([0.2, 0.6, 0.95], [0.0, 1.0, 1.0])
        cov = [r["coverage"] for r in rows]
        self.assertEqual(cov, sorted(cov, reverse=True))


class LatentBenchmarkReport(unittest.TestCase):
    def test_report_explains_rule_fallback_without_llm(self):
        report = latent_markdown(
            results={
                "naive_latest_deploy": {
                     "n": 48,
                    "detection_rate": 1.0,
                    "verified_fix_rate": 0.0,
                    "false_fix_rate": 1.0,
                    "fixed_by_llm": 0.0,
                    "mean_attempts": 1.0,
                    "mean_latency_s": 0.14,
                    "mean_tokens": 0,
                    "mean_cost_usd": 0.0,
                },
                "opspilot": {
                     "n": 48,
                    "detection_rate": 1.0,
                    "verified_fix_rate": 1.0,
                    "false_fix_rate": 0.0,
                    "fixed_by_llm": 0.0,
                    "mean_attempts": 4.0,
                    "mean_latency_s": 0.86,
                    "mean_tokens": 0,
                    "mean_cost_usd": 0.0,
                },
            },
            fam_fixed={
    "naive_latest_deploy": {
        "inventory_oversell": 0.0,
        "payments_retry_declined": 0.0,
        "payments_timeout_config": 0.0,
        "catalog_stale_cache": 0.0,
        "orders_n_plus_one": 0.0,
        "orders_rounding": 0.0,
    },
    "opspilot": {
        "inventory_oversell": 1.0,
        "payments_retry_declined": 1.0,
        "payments_timeout_config": 1.0,
        "catalog_stale_cache": 1.0,
        "orders_n_plus_one": 1.0,
        "orders_rounding": 1.0,
    },
},
            n_seeds=8,
            llm=False,
        )

        self.assertIn("rule-based repair", report)
        self.assertIn("successful fixes do not require an LLM", report)
        self.assertNotIn("can only roll back", report)
        self.assertNotIn("expected to fix nothing", report)

if __name__ == "__main__":
    unittest.main()
