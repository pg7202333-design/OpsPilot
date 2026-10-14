"""Regression suite for the healthy shop (this is the team's CI)."""
import random
import unittest

from shop.app import Shop
from shop.catalog import Catalog, SKU_PRICES
from shop.inventory import Inventory
from shop.payments import Payments, PaymentDeclined
from shop.telemetry import Telemetry


class CatalogTests(unittest.TestCase):
    def test_prices_returned(self):
        cat = Catalog(Telemetry())
        self.assertEqual(cat.get_price("sku-1"), SKU_PRICES["sku-1"])
        self.assertEqual(cat.get_prices(["sku-2", "sku-3"]),
                         {"sku-2": SKU_PRICES["sku-2"], "sku-3": SKU_PRICES["sku-3"]})

    def test_update_price_invalidates_cached_value(self):
        cat = Catalog(Telemetry())
        original = cat.get_price("sku-1")
        updated = original + 101
        cat.update_price("sku-1", updated)
        self.assertEqual(cat.get_price("sku-1"), updated)


class InventoryTests(unittest.TestCase):
    def test_reserve_and_release(self):
        inv = Inventory(Telemetry(), {"a": 3})
        self.assertTrue(inv.reserve("a", 2))
        self.assertEqual(inv.stock("a"), 1)
        inv.release("a", 2)
        self.assertEqual(inv.stock("a"), 3)

    def test_rejects_when_clearly_insufficient(self):
        inv = Inventory(Telemetry(), {"a": 3})
        self.assertFalse(inv.reserve("a", 5))
        self.assertEqual(inv.stock("a"), 3)


class PaymentsTests(unittest.TestCase):
    def test_charge_succeeds(self):
        pay = Payments(Telemetry(), random.Random(0))
        self.assertGreaterEqual(pay.charge(1001, 500), 1)

    def test_declined_card_raises(self):
        pay = Payments(Telemetry(), random.Random(0))
        with self.assertRaises(PaymentDeclined):
            pay.charge(1007, 500)


class OrderTests(unittest.TestCase):
    def test_happy_path_total(self):
        shop = Shop(seed=0)
        res = shop.handle({"type": "order", "order_id": 1001,
                           "items": [["sku-4", 2]], "promo": False})
        self.assertEqual(res["status"], "paid")
        self.assertEqual(res["total"], 24000)

    def test_exact_promo_total(self):
        shop = Shop(seed=0)
        res = shop.handle({"type": "order", "order_id": 1001,
                           "items": [["sku-4", 1]], "promo": True})
        self.assertEqual(res["total"], 10800)

    def test_out_of_stock_releases_reservations(self):
        shop = Shop(seed=0)
        before = shop.inventory.stock("sku-5")
        res = shop.handle({"type": "order", "order_id": 1001,
                           "items": [["sku-5", 2], ["sku-6", 99]], "promo": False})
        self.assertEqual(res["status"], "out_of_stock")
        self.assertEqual(shop.inventory.stock("sku-5"), before)


if __name__ == "__main__":
    unittest.main()
