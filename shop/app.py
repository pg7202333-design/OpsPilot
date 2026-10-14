"""Wires the services together. Behaviour is fully determined by (journal, seed)."""
import random

from shop.catalog import Catalog
from shop.inventory import Inventory
from shop.ledger import Ledger
from shop.orders import Orders
from shop.payments import Payments
from shop.telemetry import Telemetry


class Shop:
    def __init__(self, seed=0):
        self.tel = Telemetry()
        self.rng = random.Random(seed)
        self.catalog = Catalog(self.tel)
        self.inventory = Inventory(self.tel)
        self.payments = Payments(self.tel, self.rng)
        self.ledger = Ledger(self.tel, self.catalog)
        self.orders = Orders(self.tel, self.catalog, self.inventory, self.payments, self.ledger)

    def handle(self, event):
        if event["type"] == "order":
            items = [(sku, qty) for sku, qty in event["items"]]
            result = self.orders.place(event["order_id"], items, event.get("promo", False))
            self.tel.advance(0.05)
            return result
        if event["type"] == "price_update":
            self.catalog.update_price(event["sku"], event["price"])
            return {"status": "price_updated"}
        raise ValueError("unknown event type: %r" % (event["type"],))
