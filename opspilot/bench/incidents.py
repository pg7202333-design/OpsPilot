"""OpsBench: reproducible fault-injection incidents with ground truth and deploy history.

Each incident = healthy shop code + a deploy history. Exactly one deploy (the culprit)
introduces the fault; the others are behaviour-preserving decoys (unused locals/config whose
names deliberately overlap with incident symptoms). The incident's code state is the
healthy source with every deploy's edit applied in chronological order.
"""
import difflib
import random
from dataclasses import dataclass, field
from pathlib import Path

T0 = 1_760_000_000  # arbitrary epoch anchor
REPO_ROOT = Path(__file__).resolve().parents[2]

BASELINE_SEEDS = (4242, 4243, 4244, 4245, 4246)  # pooled healthy reference
HELDOUT_SEEDS = (5001, 5002)   # used by the verifier (visible to the agent)
ORACLE_SEEDS = (9001, 9002, 9003)  # hidden: used only to score "truly fixed"

FAULTS = {
    "inventory_oversell": dict(
        file="shop/inventory.py",
        find=("            self.tel.advance(0.004)\n"
              "            if self._stock.get(sku, 0) < qty:\n"
              "                return False\n"),
        replace="            self.tel.advance(0.004)\n",
        message="inventory: simplify reserve check",
        summary="Reservation guard removed, so stock goes negative (overselling).",
    ),
    "payments_retry_declined": dict(
        file="shop/payments.py",
        find=("                    if not err.retryable:\n"
              "                        raise PaymentDeclined(order_id)\n"
              "                    self.tel.advance(cfg[\"backoff_s\"] * attempt)\n"),
        replace="                    self.tel.advance(cfg[\"backoff_s\"] * attempt)\n",
        message="payments: streamline retry loop",
        summary="Non-retryable declines are retried with backoff, amplifying latency and errors.",
    ),
    "payments_timeout_config": dict(
        file="shop/config.py",
        find='"timeout_s": 2.0',
        replace='"timeout_s": 0.05',
        message="config: tune gateway settings",
        summary="Gateway timeout set far below normal latency, so every charge times out.",
    ),
    "catalog_stale_cache": dict(
        file="shop/catalog.py",
        find=("        self._db[sku] = price_cents\n"
              "        self._cache.pop(sku, None)\n"),
        replace="        self._db[sku] = price_cents\n",
        message="catalog: simplify price update path",
        summary="Price updates no longer invalidate the cache, so orders use stale prices.",
    ),
    "orders_n_plus_one": dict(
        file="shop/orders.py",
        find="prices = self.catalog.get_prices(skus)",
        replace="prices = {sku: self.catalog.get_price(sku) for sku in skus}",
        message="orders: simplify price lookup in order flow",
        summary="Batch price lookup replaced by one catalog call per item (N+1 queries).",
    ),
    "orders_rounding": dict(
        file="shop/orders.py",
        find="total = (subtotal * (100 - pct) + 50) // 100",
        replace="total = subtotal * (100 - pct) // 100",
        message="orders: cleanup totals calculation",
        summary="Discounted totals are floored instead of rounded half up (off by a cent).",
    ),
}

# Decoy deploys: real code edits that are behaviour-preserving (unused locals / unused config).
# Names deliberately reuse symptom vocabulary so token matching alone cannot dismiss them.
DECOYS = [
    dict(file="shop/payments.py", anchor="        cfg = self.cfg\n", before=False,
         comment="        gateway_errors = 0\n",
         message="payments: add local for error accounting"),
    dict(file="shop/inventory.py", anchor="    def reserve(self, sku, qty):\n", before=False,
         comment="        negative_stock = False\n",
         message="inventory: track stock state locally"),
    dict(file="shop/orders.py", anchor="    def place(self, order_id, items, promo=False):\n", before=False,
         comment="        ledger_total = 0\n",
         message="orders: prepare ledger bookkeeping"),
    dict(file="shop/catalog.py", anchor="    def _lookup(self, sku):\n", before=False,
         comment="        stale_cache = False\n",
         message="catalog: prepare cache freshness flag"),
    dict(file="shop/ledger.py", anchor="    def audit(self, order_id, charged, items, discount_pct):\n", before=False,
         comment="        mismatch_count = 0\n",
         message="ledger: add mismatch counter"),
    dict(file="shop/config.py", anchor="CONFIG = {\n", before=False,
         comment='    "catalog": {"ttl_s": 300},\n',
         message="config: add catalog ttl setting"),
]


@dataclass
class Incident:
    id: str
    family: str
    seed: int
    ts: float
    journal_seed: int
    deploys: list
    culprit_id: str
    summary: str
    edits: list = field(default_factory=list)  # all edits, chronological (latent fault first)
    latent: bool = False        # fault predates the deploy history: no deploy to roll back
    reference_fix: list = field(default_factory=list)  # ground truth; never shown to the agent

    @property
    def has_culprit(self):
        return bool(self.culprit_id)

    @property
    def culprit(self):
        return next((d for d in self.deploys if d["id"] == self.culprit_id), None)


def _read_sources():
    return {str(p.relative_to(REPO_ROOT)): p.read_text()
            for p in (REPO_ROOT / "shop").glob("*.py")}


def _apply(text, edit):
    if text.count(edit["find"]) != 1:
        raise ValueError("edit does not match exactly once in %s: %r" % (edit["file"], edit["find"]))
    return text.replace(edit["find"], edit["replace"])


def _diff_lines(before, after):
    out = []
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=1):
        if line.startswith(("---", "+++", "@@")):
            continue
        out.append(line)
    return out


def build_incident(family, seed, latent=False):
    """latent=True: the fault is part of the code before the deploy history starts, so the
    deploy list contains only decoys and rolling back any deploy cannot fix the incident."""
    fault = FAULTS[family]
    rng = random.Random("%s:%d%s" % (family, seed, ":latent" if latent else ""))
    t_inc = T0 + seed * 86400.0
    fault_edit = dict(file=fault["file"], find=fault["find"], replace=fault["replace"])

    entries = []
    if not latent:
        entries.append(dict(kind="culprit", hours=rng.uniform(0.5, 6.0),
                            message=fault["message"], edit=fault_edit))
    for d in rng.sample(DECOYS, rng.randint(3, 4)):
        hours = rng.uniform(0.1, 3.0) if rng.random() < 0.5 else rng.uniform(3.0, 48.0)
        repl = d["comment"] + d["anchor"] if d["before"] else d["anchor"] + d["comment"]
        entries.append(dict(kind="decoy", hours=hours, message=d["message"],
                            edit=dict(file=d["file"], find=d["anchor"], replace=repl)))
    entries.sort(key=lambda e: -e["hours"])  # oldest first

    sources = _read_sources()
    deploys, edits = [], []
    if latent:
        sources[fault["file"]] = _apply(sources[fault["file"]], fault_edit)
        edits.append(fault_edit)
    for k, e in enumerate(entries):
        edit = e["edit"]
        before = sources[edit["file"]]
        after = _apply(before, edit)
        sources[edit["file"]] = after
        deploys.append({"id": "d%d-%d" % (seed, k), "ts": t_inc - e["hours"] * 3600.0,
                        "message": e["message"], "file": edit["file"], "edits": [edit],
                        "diff": _diff_lines(before, after), "kind": e["kind"]})
        edits.append(edit)
    culprit = next((d for d in deploys if d["kind"] == "culprit"), None)
    return Incident(id="%s%s-s%d" % (family, "-latent" if latent else "", seed), family=family,
                    seed=seed, ts=t_inc, journal_seed=1000 + seed + (500 if latent else 0),
                    deploys=deploys, culprit_id=culprit["id"] if culprit else "",
                    summary=fault["summary"], edits=edits, latent=latent,
                    reference_fix=[{"file": fault["file"], "find": fault["replace"],
                                    "replace": fault["find"]}])


def build_bench(n_seeds=5, families=None, latent=False):
    fams = families or list(FAULTS)
    return [build_incident(f, s, latent=latent) for f in fams for s in range(1, n_seeds + 1)]


def public_deploys(incident):
    """What the agent may see: no ground-truth 'kind' labels."""
    return [{k: v for k, v in d.items() if k != "kind"} for d in incident.deploys]
