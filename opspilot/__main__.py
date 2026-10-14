"""CLI:
  python -m opspilot demo [incident_id] [--llm|--ollama]   one incident end to end
  python -m opspilot bench [--seeds N] [--latent] [--llm]   full benchmark
Incident ids look like payments_retry_declined-s1 or inventory_oversell-latent-s1.
--llm needs ANTHROPIC_API_KEY; --ollama uses a local model (default qwen2.5-coder:7b).
"""
import os
import sys

from opspilot.bench.incidents import FAULTS, build_bench


def demo(incident_id, use_llm, use_ollama=False):
    from opspilot.llm import AnthropicClient, OllamaClient, LLMError, prices_from_env
    from opspilot.pipeline import Context, PipelineConfig, run_incident
    bench = build_bench(3)
    latent_bench = build_bench(3, latent=True)
    inc = next((i for i in bench + latent_bench if i.id == incident_id), None)
    if inc is None:
        print("unknown incident; try one of:", ", ".join(i.id for i in (bench[:3] + latent_bench[:3])), "...")
        return 1
    cfg = PipelineConfig()
    if use_llm or use_ollama:
        if use_ollama:
            cfg.llm = OllamaClient()
        elif not os.environ.get("ANTHROPIC_API_KEY"):
            print("--llm needs ANTHROPIC_API_KEY in the environment; use --ollama for local inference")
            return 2
        else:
            cfg.llm = AnthropicClient()
        try:
            cfg.llm.preflight()
        except LLMError as exc:
            print("LLM preflight failed, nothing was run:", exc)
            return 2
        cfg.llm.usage = type(cfg.llm.usage)()   # do not count the preflight call
        if not use_ollama:
            cfg.price_in, cfg.price_out = prices_from_env()
    res = run_incident(inc, Context(), cfg, bench if not inc.latent else latent_bench)
    print(res.report)
    print("\nreproduced as failing test:", res.reproduced)
    print("attempts:", res.attempts, "| shipped:", res.shipped_id or "-", "via", res.patch_source or "-",
          "| verified:", res.verified, "| truly fixed (hidden oracle):", res.oracle_fixed)
    if res.llm_attempts:
        print("LLM attempts: %d, tokens: %d%s" % (
            res.llm_attempts, res.tokens, ", cost $%.4f" % res.cost_usd if res.cost_usd else ""))
    if res.llm_error:
        print("LLM error:", res.llm_error)
    print("ground truth:", inc.culprit_id or "no culprit deploy (latent fault)", "-", inc.summary)
    if inc.latent and not res.shipped and not (use_llm or use_ollama):
        print("note: every deploy is a decoy, so no rollback verified and nothing was shipped; "
              "rerun with --ollama or --llm to let a model propose a patch.")
    return 0


def main(argv):
    if argv and argv[0] == "demo":
        rest = [a for a in argv[1:] if a not in ("--llm", "--ollama")]
        return demo(rest[0] if rest else "%s-s1" % next(iter(FAULTS)), "--llm" in argv, "--ollama" in argv)
    if argv and argv[0] == "bench":
        from opspilot.eval import run_bench
        sys.argv = ["run_bench"] + argv[1:]
        run_bench.main()
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
