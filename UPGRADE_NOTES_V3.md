# OpsPilot Repair Diagnosis v3

This revision adds a conservative source-pattern repair fallback after the rollback and LLM attempts. It can propose small fixes for recognized code patterns (N+1 single-item reads despite a batch API, floor-only discounted totals, missing stock precondition, retries of non-retryable errors, an abnormally low timeout setting, and missing cache invalidation).

These rules do **not** bypass verification. Every candidate must still pass the incident reproduction test, the existing shop regression suite, and held-out traffic checks before being shipped. The rules inspect source code patterns and do not use incident IDs or hidden oracle labels. They are intentionally narrow and are not a substitute for generalizing to arbitrary production code.

After extracting this ZIP over an existing checkout, run:

```bash
python3 -m unittest discover -s shop_tests -v
python3 -m unittest discover -s tests -v
python3 -m opspilot demo orders_n_plus_one-latent-s1 --ollama
```

A successful incident must report both `verified: True` and `truly fixed (hidden oracle): True`.
