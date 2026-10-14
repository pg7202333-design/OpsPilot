# OpsPilot repair-diagnosis update

This revision improves how the LLM repair stage consumes incident evidence.

## Changes

- The LLM receives the observed symptom section, not the ranked root-cause hypotheses. This reduces anchoring on misleading deploy rankings and decoy vocabulary.
- Source-file ordering is based only on observed symptoms. For catalog call amplification, the order orchestration path is inspected before cache internals because repeated calls can originate in a caller loop.
- The prompt adds general diagnostic guidance for call amplification, stale values, numeric mismatches, and retry/latency symptoms.
- Added regression tests ensuring hypothesis text is excluded from the LLM prompt and cannot influence source ordering.

## Apply

From the existing project directory, overlay the ZIP contents. Keep `.git`, `.gitignore`, and `results/` as they are.

## Verify

```bash
python3 -m unittest discover -s shop_tests -v
python3 -m unittest discover -s tests -v
```

The LLM behavior remains probabilistic; rerun the same six latent incidents and count a success only when both `verified: True` and `truly fixed (hidden oracle): True`.
