# OpsPilot v6 — benchmark evidence summary

**Evidence basis:** terminal output shared by the project maintainer on 2026-10-14. The full test suite was reported locally as `Ran 49 tests ... OK`. The six demo summaries below each reported `verified: True` and `truly fixed (hidden oracle): True`.

## Results

| Latent incident | Verified | Hidden oracle | Final repair source | Notes |
|---|---:|---:|---|---|
| `orders_n_plus_one-latent-s1` | Pass | Pass | `verified-rule via rules` | LLM proposals rejected; rule repair passed. |
| `inventory_oversell-latent-s1` | Pass | Pass | `verified-rule via rules` | LLM proposals rejected; rule repair passed. |
| `payments_retry_declined-latent-s1` | Pass | Pass | `verified-rule via rules` | LLM proposals rejected; rule repair passed. |
| `payments_timeout_config-latent-s1` | Pass | Pass | `verified-rule via rules` | LLM proposals rejected; rule repair passed. |
| `catalog_stale_cache-latent-s1` | Pass | Pass | `verified-rule via rules` | Cache-bypass workaround rejected; cache invalidation repair passed. |
| `orders_rounding-latent-s1` | Pass | Pass | `verified-rule via rules` | Ledger edit and arbitrary subtotal adjustment rejected; upstream rounding repair passed. |

## Test commands

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s shop_tests -v
python3 -m unittest tests.test_rule_repairs -v
```

The maintainer's v6 run reported **49/49** tests passing in the first command. Focused repair-policy tests reported **8/8**, and shop regression tests reported **9/9**.

## Important interpretation

- This is a summary of the reported v6 runs, not an independently rerun full experiment in this document-generation environment.
- `verified: True` and `truly fixed (hidden oracle): True` are benchmark-specific checks, not a guarantee of correctness in real production.
- The final repair source was rule-based for all six demos. The model generated candidate patches, but the verifier rejected those candidates; OpsPilot's fallback path produced the passing repair.
- These six incidents are the project's current known latent benchmark set. They are not a statistically representative sample of production incidents.

## How to reproduce

Run the six commands from the README in a clean checkout with the configured Ollama model available. Save raw logs, record the Git commit hash and model name, and do not edit the code between runs. If any result differs, update this report rather than assuming the earlier result still applies.
