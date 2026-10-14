"""Catalog service: authoritative prices (in cents) behind a read-through cache."""

SKU_PRICES = {
    "sku-1": 1999, "sku-2": 4550, "sku-3": 899,
    "sku-4": 12000, "sku-5": 325, "sku-6": 7777,
}


class Catalog:
    def __init__(self, tel, prices=None):
        self.tel = tel
        self._db = dict(prices or SKU_PRICES)
        self._cache = {}

    def get_price(self, sku):
        with self.tel.span("catalog", "get_price"):
            self.tel.advance(0.015)
            return self._lookup(sku)

    def get_prices(self, skus):
        with self.tel.span("catalog", "get_prices"):
            self.tel.advance(0.02)
            return {sku: self._lookup(sku) for sku in skus}

    def _lookup(self, sku):
        if sku not in self._cache:
            self._cache[sku] = self._db[sku]
        return self._cache[sku]

    def source_price(self, sku):
        return self._db[sku]

    def update_price(self, sku, price_cents):
        self._db[sku] = price_cents
        self._cache.pop(sku, None)
        self.tel.log("catalog", "INFO", "price_updated", sku=sku, price=price_cents)
