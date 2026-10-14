# OpsPilot Repair Diagnosis v4

## What changed

- Added a safety-policy check for ledger-mismatch incidents: LLM patches that modify `shop/ledger.py` are rejected before execution. This prevents changing the independent auditor to make an incorrect charged total appear correct.
- The rejected-patch feedback tells the model to repair the upstream value-producing calculation. If LLM attempts fail, the existing verified rule candidate can restore round-half-up in `shop/orders.py`.
- Added tests for the policy and for the rounding fallback target.

## Validation

Run these commands from the extracted project directory:

```bash
python3 -m unittest discover -s shop_tests -v
python3 -m unittest discover -s tests -v
python3 -m opspilot demo orders_rounding-latent-s1 --ollama
```

A benchmark repair counts as a true success only when both `verified: True` and `truly fixed (hidden oracle): True` are reported.
