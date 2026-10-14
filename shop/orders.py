"""Orders service: orchestrates catalog, inventory, payments and the ledger."""
from shop.config import CONFIG
from shop.payments import PaymentDeclined, PaymentError


class Orders:
    def __init__(self, tel, catalog, inventory, payments, ledger):
        self.tel = tel
        self.catalog = catalog
        self.inventory = inventory
        self.payments = payments
        self.ledger = ledger

    def _release_all(self, reserved):
        for sku, qty in reserved:
            self.inventory.release(sku, qty)

    def place(self, order_id, items, promo=False):
        with self.tel.span("orders", "place_order"):
            skus = [sku for sku, _ in items]
            prices = self.catalog.get_prices(skus)
            reserved = []
            for sku, qty in items:
                if not self.inventory.reserve(sku, qty):
                    self._release_all(reserved)
                    self.tel.log("orders", "INFO", "out_of_stock", order_id=order_id, sku=sku)
                    return {"order_id": order_id, "status": "out_of_stock", "total": 0}
                reserved.append((sku, qty))
            subtotal = sum(prices[sku] * qty for sku, qty in items)
            pct = CONFIG["orders"]["promo_pct"] if promo else 0
            total = (subtotal * (100 - pct) + 50) // 100
            try:
                self.payments.charge(order_id, total)
            except PaymentDeclined:
                self._release_all(reserved)
                self.tel.log("orders", "INFO", "payment_declined", order_id=order_id)
                return {"order_id": order_id, "status": "declined", "total": 0}
            except PaymentError:
                self._release_all(reserved)
                self.tel.log("orders", "ERROR", "order_failed", order_id=order_id)
                return {"order_id": order_id, "status": "failed", "total": 0}
            self.ledger.audit(order_id, total, items, pct)
            return {"order_id": order_id, "status": "paid", "total": total}
