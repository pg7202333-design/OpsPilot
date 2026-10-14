"""Payments service: card gateway with retries and backoff."""
from shop.config import CONFIG


class PaymentError(Exception):
    pass


class PaymentDeclined(Exception):
    pass


class _GatewayError(Exception):
    def __init__(self, code, retryable):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class Payments:
    def __init__(self, tel, rng, cfg=None):
        self.tel = tel
        self.rng = rng
        self.cfg = cfg if cfg is not None else CONFIG["payments"]

    def _gateway(self, order_id):
        if order_id % 20 == 7:
            raise _GatewayError("card_declined", retryable=False)
        if self.rng.random() < 0.1:
            raise _GatewayError("gateway_503", retryable=True)

    def charge(self, order_id, amount):
        cfg = self.cfg
        with self.tel.span("payments", "charge"):
            for attempt in range(1, cfg["max_retries"] + 1):
                try:
                    latency = self.rng.uniform(0.05, 0.2)
                    if latency > cfg["timeout_s"]:
                        self.tel.advance(cfg["timeout_s"])
                        raise _GatewayError("timeout", retryable=True)
                    self.tel.advance(latency)
                    self._gateway(order_id)
                    return attempt
                except _GatewayError as err:
                    self.tel.log("payments", "WARN", "gateway_error",
                                 order_id=order_id, code=err.code,
                                 retryable=err.retryable, attempt=attempt)
                    if not err.retryable:
                        raise PaymentDeclined(order_id)
                    self.tel.advance(cfg["backoff_s"] * attempt)
            self.tel.log("payments", "ERROR", "retries_exhausted", order_id=order_id)
            raise PaymentError("retries exhausted for %s" % order_id)
