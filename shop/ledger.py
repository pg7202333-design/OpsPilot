"""Ledger service: independent finance audit of every charge."""


class Ledger:
    def __init__(self, tel, catalog):
        self.tel = tel
        self.catalog = catalog

    def audit(self, order_id, charged, items, discount_pct):
        with self.tel.span("ledger", "audit"):
            self.tel.advance(0.002)
            subtotal = sum(self.catalog.source_price(sku) * qty for sku, qty in items)
            expected = (subtotal * (100 - discount_pct) + 50) // 100
            if charged != expected:
                self.tel.log("ledger", "ERROR", "ledger_mismatch", order_id=order_id,
                             delta_cents=charged - expected, discount_pct=discount_pct)
