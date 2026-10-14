"""Deterministic workload journals (the replayable record of what traffic did)."""
import random

from shop.app import Shop
from shop.catalog import SKU_PRICES


def make_journal(seed, n_events=150):
    rng = random.Random(seed * 7919 + 13)
    skus = sorted(SKU_PRICES)
    prices = dict(SKU_PRICES)
    events = []
    order_id = 1000
    for i in range(n_events):
        if i > 0 and i % 20 == 0:
            sku = rng.choice(skus)
            prices[sku] = int(prices[sku] * rng.choice([0.9, 1.1, 1.25]))
            events.append({"type": "price_update", "sku": sku, "price": prices[sku]})
            continue
        order_id += 1
        chosen = rng.sample(skus, rng.randint(1, 4))
        items = [[sku, rng.randint(1, 3)] for sku in chosen]
        events.append({"type": "order", "order_id": order_id, "items": items,
                       "promo": rng.random() < 0.35})
    return events


def run_journal(journal, seed):
    shop = Shop(seed=seed)
    results = [shop.handle(event) for event in journal]
    return shop, results
