"""Inventory service: stock reservation."""

DEFAULT_STOCK = {
    "sku-1": 2000, "sku-2": 2000, "sku-3": 70,
    "sku-4": 2000, "sku-5": 2000, "sku-6": 55,
}


class Inventory:
    def __init__(self, tel, stock=None):
        self.tel = tel
        self._stock = dict(stock or DEFAULT_STOCK)

    def reserve(self, sku, qty):
        with self.tel.span("inventory", "reserve"):
            self.tel.advance(0.004)
            if self._stock.get(sku, 0) < qty:
                return False
            self._stock[sku] -= qty
            if self._stock[sku] < 0:
                self.tel.log("inventory", "ERROR", "negative_stock",
                             sku=sku, stock=self._stock[sku])
            return True

    def release(self, sku, qty):
        with self.tel.span("inventory", "release"):
            self.tel.advance(0.003)
            self._stock[sku] = self._stock.get(sku, 0) + qty

    def stock(self, sku):
        return self._stock.get(sku, 0)
