"""Service configuration (changes here ship as config deploys)."""

CONFIG = {
    "payments": {"timeout_s": 2.0, "max_retries": 3, "backoff_s": 0.2},
    "orders": {"promo_pct": 10},
}
