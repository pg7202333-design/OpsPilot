"""Tiny tracing/logging layer on a virtual clock (no real sleeping, fully deterministic)."""
import itertools
from contextlib import contextmanager
from dataclasses import dataclass, asdict


@dataclass
class Span:
    span_id: int
    trace_id: int
    parent_id: object
    service: str
    name: str
    start: float
    end: float = 0.0
    status: str = "ok"
    error: str = ""


class Telemetry:
    def __init__(self):
        self.now = 0.0
        self.logs = []
        self.spans = []
        self._ids = itertools.count(1)
        self._stack = []

    def advance(self, dt):
        self.now += dt

    def log(self, service, level, msg, **fields):
        trace_id = self._stack[-1].trace_id if self._stack else None
        self.logs.append(
            {"ts": round(self.now, 6), "service": service, "level": level,
             "msg": msg, "trace_id": trace_id, **fields}
        )

    @contextmanager
    def span(self, service, name):
        parent = self._stack[-1] if self._stack else None
        sid = next(self._ids)
        sp = Span(sid, parent.trace_id if parent else sid,
                  parent.span_id if parent else None, service, name, self.now)
        self.spans.append(sp)
        self._stack.append(sp)
        try:
            yield sp
        except Exception as exc:
            sp.status = "error"
            sp.error = type(exc).__name__
            raise
        finally:
            sp.end = self.now
            self._stack.pop()

    def export(self):
        return {"logs": list(self.logs), "spans": [asdict(s) for s in self.spans]}
