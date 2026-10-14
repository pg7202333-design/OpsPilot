import unittest

from opspilot.repair import patch_policy_rejection, rule_based_candidates


class RuleRepairCandidates(unittest.TestCase):
    def test_n_plus_one_prefers_existing_batch_api(self):
        sources = {
            "shop/orders.py": "prices = {sku: self.catalog.get_price(sku) for sku in skus}\n",
            "shop/catalog.py": "    def get_prices(self, skus):\n        return {}\n",
        }
        candidates = rule_based_candidates("calls_per_request catalog", sources)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["file"], "shop/orders.py")
        self.assertEqual(candidates[0]["replace"], "prices = self.catalog.get_prices(skus)")

    def test_rounding_candidate_restores_half_up_rule(self):
        sources = {
            "shop/orders.py": "total = subtotal * (100 - pct) // 100\n",
        }
        candidates = rule_based_candidates("ledger_mismatch", sources)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["replace"], "total = (subtotal * (100 - pct) + 50) // 100")

    def test_rounding_repair_targets_order_calculation_not_ledger(self):
        sources = {
            "shop/orders.py": "total = subtotal * (100 - pct) // 100\n",
            "shop/ledger.py": "expected = (subtotal * (100 - discount_pct) + 50) // 100\n",
        }
        candidates = rule_based_candidates("ledger_mismatch", sources)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["file"], "shop/orders.py")
        self.assertEqual(candidates[0]["replace"], "total = (subtotal * (100 - pct) + 50) // 100")

    def test_rejects_ledger_auditor_edits_for_mismatch_symptom(self):
        report = "## Detected symptoms\n- log_signature ledger [ERROR:ledger_mismatch]"
        edits = [{"file": "shop/ledger.py", "find": "expected = old", "replace": "expected = new"}]
        self.assertIn("independent auditor", patch_policy_rejection(report, edits))

    def test_allows_upstream_order_fix_for_mismatch_symptom(self):
        report = "## Detected symptoms\n- log_signature ledger [ERROR:ledger_mismatch]"
        edits = [{"file": "shop/orders.py", "find": "total = old", "replace": "total = new"}]
        self.assertEqual(patch_policy_rejection(report, edits), "")

    def test_rejects_order_path_bypass_of_catalog_cache(self):
        report = "## Detected symptoms\n- log_signature ledger [ERROR:ledger_mismatch]"
        edits = [{
            "file": "shop/orders.py",
            "find": "subtotal = sum(prices[sku] * qty for sku, qty in items)",
            "replace": "subtotal = sum(self.catalog.source_price(sku) * qty for sku, qty in items)",
        }]
        self.assertIn("do not bypass the catalog cache", patch_policy_rejection(report, edits))

    def test_stale_cache_candidate_restores_invalidation(self):
        sources = {
            "shop/catalog.py": (
                "    def update_price(self, sku, price_cents):\n"
                "        self._db[sku] = price_cents\n"
                "        self.tel.log(\"catalog\", \"INFO\", \"price_updated\", sku=sku, price=price_cents)\n"
            )
        }
        candidates = rule_based_candidates("ledger_mismatch", sources)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["file"], "shop/catalog.py")
        self.assertIn("self._cache.pop(sku, None)", candidates[0]["replace"])

    def test_no_candidate_for_already_healthy_order_code(self):
        sources = {
            "shop/orders.py": "prices = self.catalog.get_prices(skus)\n"
                              "total = (subtotal * (100 - pct) + 50) // 100\n",
            "shop/catalog.py": "    def get_prices(self, skus):\n        return {}\n",
        }
        self.assertEqual(rule_based_candidates("healthy", sources), [])


if __name__ == "__main__":
    unittest.main()
