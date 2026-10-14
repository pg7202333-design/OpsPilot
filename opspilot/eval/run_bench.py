"""Run OpsBench: baselines, OpsPilot and ablations. Writes results/results.json and RESULTS.md.

    python -m opspilot.eval.run_bench --seeds 8 --out results
    python -m opspilot.eval.run_bench --seeds 8 --out results --latent            # rollback only
    ANTHROPIC_API_KEY=... python -m opspilot.eval.run_bench --latent --llm        # + LLM patcher
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

from opspilot.bench.incidents import FAULTS, build_bench, public_deploys
from opspilot.eval.metrics import bootstrap_ci, ece, mean, reliability, risk_coverage
from opspilot.investigate import investigate
from opspilot.llm import AnthropicClient, LLMError, prices_from_env
from opspilot.pipeline import Context, PipelineConfig, run_incident
from opspilot.sandbox import Sandbox

RECENCY_ONLY = {"proximity": 0.0, "overlap": 0.0, "memory": 0.0, "recency": 1.0}

SYSTEMS = {
    "naive_latest_deploy": PipelineConfig("naive_latest_deploy", use_memory=False,
                                          weights=RECENCY_ONLY, verify=False, top_k=1),
    "opspilot": PipelineConfig("opspilot"),
    "no_verify": PipelineConfig("no_verify", verify=False, top_k=1),
    "no_repro_test": PipelineConfig("no_repro_test", use_repro=False),
    "no_memory": PipelineConfig("no_memory", use_memory=False),
    "no_localization": PipelineConfig("no_localization", weights={"proximity": 0.0}),
    "no_overlap": PipelineConfig("no_overlap", weights={"overlap": 0.0}),
    "opspilot_llm": PipelineConfig("opspilot_llm"),  # a client is attached in the worker
}


def _run_system(args):
    name, n_seeds, latent = args
    bench = build_bench(n_seeds, latent=latent)
    ctx = Context()
    cfg = SYSTEMS[name]
    if name == "opspilot_llm":
        p_in, p_out = prices_from_env()
        cfg = replace(cfg, llm=AnthropicClient(), price_in=p_in, price_out=p_out)
    return name, [asdict(run_incident(inc, ctx, cfg, bench)) for inc in bench]


def summarize_system(rows):
    n = len(rows)
    detected = [r for r in rows if r["detected"]]
    shipped = [r for r in rows if r["shipped"]]
    with_c = [r for r in rows if r["has_culprit"]]      # diagnosis only exists for these
    top1 = [float(r["top1_correct"]) for r in with_c]
    fixed = [float(r["oracle_fixed"]) for r in rows]
    wrong_ships = [r for r in shipped if not r["oracle_fixed"]]
    det_c = [r for r in with_c if r["detected"]]
    confs = [r["confidence"] for r in det_c]
    correct = [float(r["top1_correct"]) for r in det_c]
    return {
        "n": n,
        "detection_rate": round(len(detected) / n, 3),
        "top1": round(mean(top1), 3) if top1 else None,
        "top1_ci": [round(x, 3) for x in bootstrap_ci(top1)] if top1 else None,
        "top3": round(mean(float(r["top3_correct"]) for r in with_c), 3) if with_c else None,
        "verified_fix_rate": round(mean(fixed), 3),
        "verified_fix_ci": [round(x, 3) for x in bootstrap_ci(fixed)],
        "shipped_rate": round(len(shipped) / n, 3),
        "false_fix_rate": round(len(wrong_ships) / len(shipped), 3) if shipped else None,
        "fixed_by_llm": round(mean(float(r["patch_source"] == "llm" and r["oracle_fixed"])
                                   for r in rows), 3),
        "mean_attempts": round(mean(r["attempts"] for r in rows), 2),
        "mean_latency_s": round(mean(r["latency_s"] for r in rows), 2),
        "mean_tokens": round(mean(r["tokens"] for r in rows)),
        "mean_cost_usd": round(mean(r["cost_usd"] for r in rows), 4),
        "ece_top1": round(ece(confs, correct), 3) if det_c else None,
    }


def per_family(rows):
    out = {}
    for fam in FAULTS:
        sub = [r for r in rows if r["family"] == fam]
        out[fam] = {"top1": round(mean(float(r["top1_correct"]) for r in sub), 2),
                    "fixed": round(mean(float(r["oracle_fixed"]) for r in sub), 2)}
    return out


def per_round(rows):
    rounds = sorted({r["seed"] for r in rows})
    return {s: round(mean(float(r["top1_correct"]) for r in rows if r["seed"] == s), 2)
            for s in rounds}


def calibration_study(n_seeds, temps=(0.1, 0.25, 0.5, 1.0, 2.0)):
    bench = build_bench(n_seeds)
    ctx = Context()
    table = {}
    for t in temps:
        confs, correct, seeds = [], [], []
        for inc in bench:
            obs, an = ctx.observe(inc)
            h = investigate(an, obs, public_deploys(inc), inc.ts,
                            memory=ctx.memory_for(inc, bench), temperature=t)
            confs.append(h[0].confidence)
            correct.append(float(h[0].deploy_id == inc.culprit_id))
            seeds.append(inc.seed)
        cal = [i for i, s in enumerate(seeds) if s <= 2]
        test = [i for i, s in enumerate(seeds) if s > 2]
        table[t] = {
            "ece_calibration_split": round(ece([confs[i] for i in cal], [correct[i] for i in cal]), 3),
            "ece_test_split": round(ece([confs[i] for i in test], [correct[i] for i in test]), 3),
            "confs": confs, "correct": correct, "test": test,
        }
    best_t = min(temps, key=lambda t: table[t]["ece_calibration_split"])
    best = table[best_t]
    reliability_rows = reliability([best["confs"][i] for i in best["test"]],
                                   [best["correct"][i] for i in best["test"]])
    return {"temperatures": {str(t): {k: v for k, v in d.items()
                                      if k.startswith("ece")} for t, d in table.items()},
            "chosen_temperature": best_t, "reliability_test_split": reliability_rows}


def ci_study(n_seeds=1):
    """Would the team's existing regression suite have caught each fault?"""
    out = {}
    for inc in build_bench(n_seeds):
        with Sandbox(edits=inc.edits) as sb:
            out[inc.family] = not sb.run_tests("shop_tests").ok
    return out


def to_markdown(results, families, rounds, cal, ci, risk, n_seeds):
    cols = ["detection_rate", "top1", "top3", "verified_fix_rate", "false_fix_rate",
            "mean_attempts", "mean_latency_s", "mean_cost_usd", "ece_top1"]
    lines = ["# OpsBench results", "",
             "%d incidents (%d fault families x %d seeds). Verified-fix = shipped patch makes the "
             "build behave identically to the healthy build on hidden traffic." % (
                 results["opspilot"]["n"], len(FAULTS), n_seeds), "",
             "| system | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for name, s in results.items():
        lines.append("| %s | %s |" % (name, " | ".join(
            "n/a" if s[c] is None else str(s[c]) for c in cols)))
    lines += ["", "95%% bootstrap CIs - OpsPilot top-1: %s, verified-fix: %s" % (
        results["opspilot"]["top1_ci"], results["opspilot"]["verified_fix_ci"]), "",
        "## Per-family (OpsPilot)", "", "| family | top1 | fixed | CI would catch |", "|---|---|---|---|"]
    for fam, d in families.items():
        lines.append("| %s | %s | %s | %s |" % (fam, d["top1"], d["fixed"], ci[fam]))
    lines += ["", "## Memory learning curve (top-1 by incident-stream round)", "",
              "| round | with memory | no memory |", "|---|---|---|"]
    for s in rounds["opspilot"]:
        lines.append("| %s | %s | %s |" % (s, rounds["opspilot"][s], rounds["no_memory"][s]))
    lines += ["", "## Calibration of top-1 confidence", "",
              "Chosen temperature (fit on rounds 1-2): %s" % cal["chosen_temperature"], "",
              "| temperature | ECE (rounds 1-2) | ECE (rounds 3+) |", "|---|---|---|"]
    for t, d in cal["temperatures"].items():
        lines.append("| %s | %s | %s |" % (t, d["ece_calibration_split"], d["ece_test_split"]))
    lines += ["", "## Selective shipping without verification (no_verify system)", "",
              "| confidence threshold | coverage | wrong-fix risk |", "|---|---|---|"]
    for r in risk:
        lines.append("| %s | %s | %s |" % (r["threshold"], r["coverage"], r["risk"]))
    return "\n".join(lines) + "\n"


def latent_markdown(results, fam_fixed, n_seeds, llm):
    cols = ["detection_rate", "verified_fix_rate", "false_fix_rate", "fixed_by_llm",
            "mean_attempts", "mean_latency_s", "mean_tokens", "mean_cost_usd"]
    lines = ["# OpsBench: latent-bug split", "",
             "%d incidents (%d fault families x %d seeds). The fault predates the deploy history, "
             "so every deploy is a decoy and rolling one back cannot fix the incident." % (
                 results["opspilot"]["n"], len(FAULTS), n_seeds), "",
             "| system | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for name, s in results.items():
        lines.append("| %s | %s |" % (name, " | ".join(
            "n/a" if s[c] is None else str(s[c]) for c in cols)))
    lines += ["", "## Verified-fix rate per family", "",
              "| family | " + " | ".join(fam_fixed) + " |", "|---" * (len(fam_fixed) + 1) + "|"]
    for fam in FAULTS:
        lines.append("| %s | %s |" % (fam, " | ".join(str(fam_fixed[n][fam]) for n in fam_fixed)))
    if not llm:
        lines += ["", "No LLM was attached in this run, so `opspilot` can only roll back and is "
                  "expected to fix nothing here. Re-run with `--llm` and an API key to measure "
                  "the LLM patcher."]
    return "\n".join(lines) + "\n"


def run_latent(args):
    names = ["naive_latest_deploy", "opspilot"] + (["opspilot_llm"] if args.llm else [])
    workers = 1 if args.llm else args.workers   # keep API concurrency polite
    with ProcessPoolExecutor(max_workers=workers) as ex:
        runs = dict(ex.map(_run_system, [(n, args.seeds, True) for n in names]))
    results = {n: summarize_system(r) for n, r in runs.items()}
    fam_fixed = {n: {fam: round(mean(float(r["oracle_fixed"]) for r in rows if r["family"] == fam), 2)
                     for fam in FAULTS} for n, rows in runs.items()}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results_latent.json").write_text(json.dumps(
        {"summary": results, "per_family_fixed": fam_fixed, "rows": runs}, indent=1, default=str))
    md = latent_markdown(results, fam_fixed, args.seeds, args.llm)
    (out / "RESULTS_LATENT.md").write_text(md)
    print(md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--out", default="results")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--latent", action="store_true", help="run the latent-bug split")
    ap.add_argument("--llm", action="store_true", help="attach the Anthropic LLM patcher (needs ANTHROPIC_API_KEY)")
    args = ap.parse_args()
    if args.llm and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("--llm needs ANTHROPIC_API_KEY in the environment")
    if args.llm and not args.latent:
        sys.exit("--llm only matters on the latent split; add --latent")
    if args.llm:
        try:
            AnthropicClient().preflight()
        except LLMError as exc:
            sys.exit("LLM preflight failed, nothing was run: %s" % exc)
    start = time.time()
    if args.latent:
        run_latent(args)
        print("done in %.0fs" % (time.time() - start))
        return

    names = [n for n in SYSTEMS if n != "opspilot_llm"]
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        runs = dict(ex.map(_run_system, [(n, args.seeds, False) for n in names]))

    results = {n: summarize_system(r) for n, r in runs.items()}
    families = per_family(runs["opspilot"])
    rounds = {n: per_round(runs[n]) for n in ("opspilot", "no_memory")}
    cal = calibration_study(args.seeds)
    ci = ci_study()
    nv = [r for r in runs["no_verify"] if r["detected"]]
    # selective shipping: ship only when confidence >= threshold; risk = shipped-but-not-fixed
    risk = risk_coverage([r["confidence"] for r in nv], [float(r["oracle_fixed"]) for r in nv])

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(
        {"summary": results, "per_family": families, "rounds": rounds, "calibration": cal,
         "ci_catches": ci, "risk_coverage": risk, "rows": runs}, indent=1, default=str))
    md = to_markdown(results, families, rounds, cal, ci, risk, args.seeds)
    (out / "RESULTS.md").write_text(md)
    print(md)
    print("done in %.0fs" % (time.time() - start))


if __name__ == "__main__":
    main()
