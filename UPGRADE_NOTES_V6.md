# OpsPilot v6 upgrade notes

## Cache-invariant protection

- Added a repair policy rejecting order-path patches that bypass the catalog cache with `source_price()`.
- The policy directs stale-price repairs toward cache invalidation in `Catalog.update_price()`, preserving the catalog contract for all consumers.
- Added a policy regression test and a catalog unit test proving that a cached price refreshes after `update_price()`.
- The existing rule-based candidate restores `self._cache.pop(sku, None)` when it is missing; normal verification still decides whether the candidate ships.

## Verification

Run from the project root:

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s shop_tests -v
python3 -m py_compile opspilot/*.py shop/*.py
```

The package has been checked with these tests in the build environment. Re-run the six live Ollama incidents on your Mac to confirm the behavior with your exact local model and runtime.
