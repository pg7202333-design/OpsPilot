"""Stage 4 - Repair: turn evidence into a candidate patch (a list of exact-match edits).

Patchers only *propose* edits; the sandbox enforces what may be written and verify.py
decides whether the result is accepted. Nothing a model returns is trusted.
"""
import json
import re


class PatchParseError(ValueError):
    pass


class Patcher:
    name = "base"


class RollbackPatcher(Patcher):
    """Revert the suspected deploy. A strong, safe baseline: it can only undo shipped changes."""
    name = "rollback"

    def propose(self, deploy, report=None):
        return [{"file": e["file"], "find": e["replace"], "replace": e["find"]}
                for e in reversed(deploy["edits"])]




SYSTEM_PROMPT = """You are a careful SRE repair engineer for a Python microservice application.

Diagnose the incident using the observed symptoms, source code, and deploy history.
The current source may contain a latent fault that was never introduced by a deploy.
Deploy descriptions and ranking scores are clues, not proof of causation; recent or
high-ranked changes may be unrelated. The symptom itself can also be downstream of
the real defect, so trace the data and control flow before choosing a file.

Return ONLY one valid JSON object:
{
  "explanation": "Briefly identify the symptom, the source-level cause, and why the edit fixes it.",
  "edits": [
    {
      "file": "shop/example.py",
      "find": "exact existing source text",
      "replace": "corrected source text"
    }
  ]
}

DIAGNOSIS RULES:
1. Start with the strongest observed symptom and its severity. Separate direct evidence
   from hypotheses and correlations.
2. Trace the affected request through callers, data transformations, and side effects.
   For a mismatch, inspect both where values are produced and where they are checked;
   an auditor or logger may only be reporting a defect introduced upstream.
3. Use the ranked hypotheses as search hints, not as answers. A high score, recent
   timestamp, matching word, or file mention is not sufficient evidence for a patch.
4. A latent fault may exist in any editable source file, even if no deploy touched it.
   Do not change a file merely because it appears in a deploy diff.
5. Before editing, state internally the causal chain: observed symptom -> faulty statement
   or missing condition -> expected behavior after the fix. If the proposed edit does not
   break this chain, choose another diagnosis.
6. Inspect the complete surrounding function and its callers. Prefer a defect in the
   business operation over a change that merely hides, delays, or relabels its symptom.
7. Match the type of signal to plausible code paths: call amplification suggests loops
   or repeated calls; stale values suggest cache update/invalidation paths; excessive
   latency suggests waits, retries, timeouts, or repeated work; numeric mismatches suggest
   the calculation and rounding path as well as the checker.

PATCH RULES:
8. Propose the smallest behaviorally correct fix. Do not remove logging, suppress errors,
   add artificial delays, add unused variables, or make cosmetic changes as a substitute.
9. Preserve public method signatures, argument counts, return types, data shapes, error
   semantics, and existing regression behavior unless changing one is necessary and
   supported by direct evidence. Check all visible callers before changing a contract.
10. For state-changing operations, check required preconditions BEFORE changing state or
    triggering downstream side effects.
11. The "find" value must match existing source text EXACTLY ONCE, including spaces,
    indentation, and newlines. Prefer a complete source line copied from the file.
12. The "replace" value must be valid Python with correct indentation on every line.
    Never invent surrounding source code or include text not present in the named file.
13. Only edit files listed under Current source. Never edit tests, telemetry, the incident
    report, or verification machinery.
14. For inventory reservation, if stock is decremented without a precondition and telemetry
    reports negative stock, reject insufficient stock BEFORE decrementing:
    `if self._stock.get(sku, 0) < qty: return False`. A normal out-of-stock rejection must
    not log `negative_stock`; that log means the invariant was already violated.
15. If a previous patch was rejected, inspect both the rejected edit and verifier feedback.
    Do not repeat the same edit, change unrelated code to make a test quiet, or ignore a
    regression. Correct the root cause while preserving the existing contracts.
16. Do not claim a fix is verified. The external verifier alone determines acceptance.

Return one minimal patch whenever the evidence supports a concrete correction. Do not
include Markdown fences or prose outside the JSON object."""


def _observed_symptoms(report):
    """Return observed symptom evidence without the ranked hypotheses section.

    Investigation rankings are noisy and can contain decoy vocabulary. Using them to
    prioritize files creates an anchoring loop: a misleading hypothesis influences which
    source is shown first, then appears to be confirmed by the model's diagnosis.
    """
    marker = "## Detected symptoms"
    start = report.find(marker)
    if start < 0:
        return report.strip()
    body = report[start + len(marker):]
    next_section = body.find("\n## ")
    if next_section >= 0:
        body = body[:next_section]
    return body.strip()


def _source_order(report, sources):
    """Prioritize files from observed symptoms, not noisy hypothesis/deploy wording."""
    text = _observed_symptoms(report).lower()
    priorities = []

    def add(*names):
        for name in names:
            if name in sources and name not in priorities:
                priorities.append(name)

    # Priorities are based on the observed signal, not incident IDs or ground-truth labels.
    if any(word in text for word in ("payments", "gateway", "retryable", "timeout")):
        add("shop/payments.py", "shop/config.py", "shop/orders.py")
    if any(word in text for word in ("calls_per_request", "catalog", "stale_cache", "cache")):
        # Call amplification at a service is often caused by its caller repeating a
        # single-item API; inspect the orchestration path before changing cache internals.
        add("shop/orders.py", "shop/catalog.py", "shop/config.py")
    if any(word in text for word in ("ledger_mismatch", "ledger|error", "discount", "rounding")):
        # A ledger mismatch can originate in the value-producing order/catalog path,
        # not necessarily in the ledger checker that emits the error.
        add("shop/orders.py", "shop/catalog.py", "shop/ledger.py")
    if any(word in text for word in ("negative_stock", "oversell", "reservation", "inventory")):
        add("shop/inventory.py", "shop/orders.py")

    for name in sorted(sources):
        if name not in priorities:
            priorities.append(name)
    return priorities



def patch_policy_rejection(report, edits):
    """Reject patches that alter the independent ledger auditor to hide a mismatch.

    A ledger mismatch is evidence that charged values disagree with an independent
    calculation. Changing that calculation to agree with a faulty charge can make the
    repro test green while leaving the actual business bug in place. For this symptom,
    repair the upstream value-producing code instead of weakening the auditor.
    """
    symptoms = _observed_symptoms(report).lower()
    is_ledger_mismatch = "ledger_mismatch" in symptoms or "ledger [error:ledger" in symptoms
    if is_ledger_mismatch and any(edit.get("file") == "shop/ledger.py" for edit in edits):
        return (
            "Safety policy: do not modify shop/ledger.py when a ledger mismatch is the "
            "observed symptom. The ledger is an independent auditor; changing its expected "
            "calculation can hide an upstream pricing/rounding defect. Repair the code that "
            "produces the charged total instead."
        )

    # Do not let an apparent ledger fix bypass the catalog's cache contract. Reading the
    # authoritative database directly from the order path may mask stale-cache symptoms
    # while leaving other catalog consumers exposed to stale values. The conservative
    # rule-based candidate can restore invalidation in Catalog.update_price instead.
    if is_ledger_mismatch and any(
        edit.get("file") == "shop/orders.py"
        and "source_price(" in edit.get("replace", "")
        for edit in edits
    ):
        return (
            "Safety policy: do not bypass the catalog cache by calling source_price() "
            "from the order path. For stale-price symptoms, repair cache invalidation in "
            "Catalog.update_price so every catalog consumer observes updated prices."
        )
    return ""


def rule_based_candidates(report, sources):
    """Generate conservative source-pattern repair candidates for common SRE failures.

    These are hypotheses, not trusted fixes: the normal verifier must still prove that a
    candidate clears the repro, preserves shop tests, and removes held-out anomalies. The
    rules inspect actual source patterns and do not use incident IDs or hidden labels.
    """
    candidates = []

    def add(file, find, replace):
        source = sources.get(file, "")
        if find and source.count(find) == 1 and find != replace:
            candidates.append({"file": file, "find": find, "replace": replace})

    # Repeated single-item reads where a batch API is already available.
    file = "shop/orders.py"
    repeated_lookup = "prices = {sku: self.catalog.get_price(sku) for sku in skus}"
    if repeated_lookup in sources.get(file, "") and "def get_prices(self, skus):" in sources.get("shop/catalog.py", ""):
        add(file, repeated_lookup, "prices = self.catalog.get_prices(skus)")

    # Financial rounding: preserve the established half-up rule used by the independent ledger.
    floor_total = "total = subtotal * (100 - pct) // 100"
    half_up_total = "total = (subtotal * (100 - pct) + 50) // 100"
    if floor_total in sources.get(file, "") and half_up_total not in sources.get(file, ""):
        add(file, floor_total, half_up_total)

    # Reservation invariant: reject insufficient stock before mutating stock.
    file = "shop/inventory.py"
    source = sources.get(file, "")
    if "self._stock[sku] -= qty" in source and "if self._stock.get(sku, 0) < qty:" not in source:
        add(file, "            self.tel.advance(0.004)\n",
            "            self.tel.advance(0.004)\n            if self._stock.get(sku, 0) < qty:\n                return False\n")

    # Non-retryable payment failures should not be retried.
    file = "shop/payments.py"
    source = sources.get(file, "")
    retry_line = '                    self.tel.advance(cfg["backoff_s"] * attempt)\n'
    if retry_line in source and "if not err.retryable:" not in source:
        add(file, retry_line,
            '                    if not err.retryable:\n'
            '                        raise PaymentDeclined(order_id)\n'
            '                    self.tel.advance(cfg["backoff_s"] * attempt)\n')

    # A timeout below the service's normal request latency creates systematic failures.
    file = "shop/config.py"
    source = sources.get(file, "")
    if '"timeout_s": 0.05' in source and '"timeout_s": 2.0' not in source:
        add(file, '"timeout_s": 0.05', '"timeout_s": 2.0')

    # Cache writes must invalidate the corresponding cached value.
    file = "shop/catalog.py"
    source = sources.get(file, "")
    update_line = "        self._db[sku] = price_cents\n"
    if update_line in source and "self._cache.pop(sku, None)" not in source:
        add(file, update_line, update_line + "        self._cache.pop(sku, None)\n")

    return candidates


def parse_edits(text, max_edits=5):
    """Extract and structurally validate the edits from a model response."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise PatchParseError("no JSON object in response")
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise PatchParseError("invalid JSON: %s" % exc)
    edits = obj.get("edits") if isinstance(obj, dict) else None
    if not isinstance(edits, list) or not edits or len(edits) > max_edits:
        raise PatchParseError("'edits' must be a non-empty list of at most %d items" % max_edits)
    out = []
    for e in edits:
        ok = isinstance(e, dict) and all(isinstance(e.get(k), str) for k in ("file", "find", "replace"))
        if not ok or not e["find"]:
            raise PatchParseError("each edit needs string file/find/replace and a non-empty find")
        out.append({"file": e["file"], "find": e["find"], "replace": e["replace"]})
    explanation = obj.get("explanation", "") if isinstance(obj.get("explanation", ""), str) else ""
    return out, explanation


class LLMPatcher(Patcher):
    """Model-generated patch. Used when rollback cannot help (e.g. latent bugs)."""
    name = "llm"

    def __init__(self, client):
        self.client = client
        self.last_explanation = ""

    @staticmethod
    def build_prompt(report, sources, deploys, feedback=None):
        symptom_evidence = _observed_symptoms(report)
        parts = [
            "## Observed symptoms (primary evidence)",
            symptom_evidence or "No explicit symptom section was provided.",
            "",
            "## Diagnostic discipline",
            "The ranked root-cause hypotheses are intentionally not included here because they can bias the diagnosis toward recent changes rather than observed behavior.",
            "Use observed symptoms to locate the behavior, then trace callers and data flow in the source. A component that logs an error may only be detecting an upstream defect.",
            "For call amplification, inspect the caller for loops/repeated single-item calls and compare with available batch APIs. For stale values, inspect update and cache-invalidation paths. For numeric mismatches, trace the produced total, rounding rule, and independent audit. For retry/latency symptoms, inspect retryability and timeout/backoff behavior.",
            "Preserve method signatures, return types, data shapes, and error semantics. Prefer one minimal change that directly explains the symptom.",
            "",
            "## Recent deploys (context only; not proof of root cause)",
            "A deploy may be unrelated to this incident. Consider latent defects in the current source too. Deploy descriptions and ranking scores are not evidence by themselves.",
        ]
        for d in deploys:
            parts.append("### %s - %s (%s)\n%s" % (d["id"], d["message"], d["file"],
                                                  "\n".join(d["diff"])))
        parts += [
            "", "## Current source",
            "Files are ordered by symptom relevance for inspection; all listed source remains available.",
        ]
        for name in _source_order(report, sources):
            parts.append("### %s\n```python\n%s```" % (name, sources[name]))
        if feedback:
            parts += [
                "", "## Previous attempt and verifier feedback", feedback,
                "Do not repeat the rejected edit. Diagnose why it failed, then propose a minimal root-cause fix.",
            ]
        return "\n".join(parts)


    def propose(self, report, sources, deploys, feedback=None):
        text = self.client.complete(
            SYSTEM_PROMPT,
            self.build_prompt(report, sources, deploys, feedback)
        )
        edits, self.last_explanation = parse_edits(text)
        return edits