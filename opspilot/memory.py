"""Incident memory: resolved incidents retrieved by symptom similarity (TF-IDF cosine)."""
import math
import re
from collections import Counter
from dataclasses import dataclass

_STOP = {"the", "and", "for", "not", "none", "true", "false", "self", "def", "return", "with"}


def tokens(text):
    """Lowercased word tokens (underscores split, naive plural stemming)."""
    out = set()
    for t in re.findall(r"[a-z]+", text.lower().replace("_", " ")):
        if len(t) > 2 and t not in _STOP:
            out.add(t[:-1] if t.endswith("s") and len(t) > 3 else t)
    return out


def token_list(text):
    out = []
    for t in re.findall(r"[a-z]+", text.lower().replace("_", " ")):
        if len(t) > 2 and t not in _STOP:
            out.append(t[:-1] if t.endswith("s") and len(t) > 3 else t)
    return out


@dataclass
class PastIncident:
    incident_id: str
    symptoms: str
    root_cause: str    # text of the culprit change (message + diff)
    fix: str


class IncidentMemory:
    def __init__(self):
        self.entries = []

    def add(self, entry):
        self.entries.append(entry)

    def __len__(self):
        return len(self.entries)

    def query(self, symptoms, k=3, min_sim=0.2):
        if not self.entries:
            return []
        docs = [Counter(token_list(e.symptoms)) for e in self.entries]
        df = Counter(t for d in docs for t in d)
        n = len(docs) + 1

        def vec(counter):
            return {t: c * (math.log(n / (1 + df.get(t, 0))) + 1.0) for t, c in counter.items()}

        def cos(a, b):
            dot = sum(a[t] * b.get(t, 0.0) for t in a)
            na = math.sqrt(sum(v * v for v in a.values()))
            nb = math.sqrt(sum(v * v for v in b.values()))
            return dot / (na * nb) if na and nb else 0.0

        q = vec(Counter(token_list(symptoms)))
        scored = sorted(((cos(q, vec(d)), e) for d, e in zip(docs, self.entries)),
                        key=lambda x: -x[0])
        return [(s, e) for s, e in scored[:k] if s >= min_sim]
