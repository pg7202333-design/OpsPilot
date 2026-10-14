
"""Minimal LLM client layer: Anthropic API, Ollama, and a deterministic test double.

Usage accounting is kept per client so the pipeline can report tokens and costs.
"""
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5-5"
_RETRYABLE = (408, 429, 500, 502, 503, 529)


class LLMError(Exception):
    pass


CERT_HINT = (
    "TLS certificate verification failed. python.org installs of Python on macOS "
    "ship without root certificates. Fix: run 'Install Certificates.command' "
    "from your Python folder, or run: pip3 install certifi"
)


def _ssl_context():
    """Prefer certifi's CA bundle when installed; otherwise use the system default."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, n_in, n_out):
        self.input_tokens += n_in
        self.output_tokens += n_out
        self.calls += 1

    def snapshot(self):
        return self.input_tokens, self.output_tokens


def cost_usd(delta_in, delta_out, price_in=None, price_out=None):
    """Return USD cost for token usage, or None when prices are unknown."""
    if price_in is None or price_out is None:
        return None
    return (delta_in * price_in + delta_out * price_out) / 1_000_000


def prices_from_env():
    def get(name):
        try:
            return float(os.environ[name])
        except (KeyError, ValueError):
            return None

    return get("OPSPILOT_PRICE_IN"), get("OPSPILOT_PRICE_OUT")


class AnthropicClient:
    def __init__(self, api_key=None, model=None, max_tokens=2048,
                 timeout=120, retries=2):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")

        self.model = model or os.environ.get("OPSPILOT_MODEL", DEFAULT_MODEL)
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.retries = retries
        self.usage = Usage()
        self._ctx = _ssl_context()

    def preflight(self):
        """Make a tiny request to check the API key and connection."""
        self.complete("Reply with the single word OK.", "ping", max_tokens=8)

    def complete(self, system, user, max_tokens=None):
        # Keep Anthropic's own request format here.
        body = json.dumps({
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "system": system,
            "messages": [
                {"role": "user", "content": user}
            ],
        }).encode()

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

        data, last = None, ""

        for attempt in range(self.retries + 1):
            req = urllib.request.Request(
                API_URL,
                data=body,
                headers=headers,
                method="POST",
            )

            try:
                with urllib.request.urlopen(
                    req, timeout=self.timeout, context=self._ctx
                ) as resp:
                    data = json.loads(resp.read())
                break

            except urllib.error.HTTPError as exc:
                last = "HTTP %d: %s" % (
                    exc.code,
                    exc.read()[:300].decode("utf-8", "replace"),
                )
                if exc.code not in _RETRYABLE:
                    raise LLMError(last) from exc

            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, ssl.SSLCertVerificationError):
                    raise LLMError(CERT_HINT) from exc
                last = "network error: %s" % exc

            if attempt < self.retries:
                time.sleep(2 ** attempt)

        if data is None:
            raise LLMError(last or "no response")

        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )

        usage = data.get("usage", {})
        self.usage.add(
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
        )
        return text


class ScriptedClient:
    """Deterministic test double for testing the repair pipeline."""

    model = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []
        self.usage = Usage()

    def complete(self, system, user, max_tokens=None):
        self.prompts.append((system, user))
        item = self.responses.pop(0) if self.responses else "{}"
        text = item(user) if callable(item) else item
        self.usage.add(len(user) // 4, len(text) // 4)
        return text


class OllamaClient:
    """Local Ollama chat client; does not require a hosted API key."""

    def __init__(self, model=None, host=None, timeout=180):
        self.model = (
            model
            or os.environ.get("OPSPILOT_OLLAMA_MODEL", "qwen2.5-coder:7b")
        )
        self.host = (
            host or os.environ.get(
                "OLLAMA_HOST", "http://127.0.0.1:11434"
            )
        ).rstrip("/")
        self.timeout = timeout
        self.usage = Usage()

    def preflight(self):
        """Check that the local model responds."""
        self.complete("Reply with the single word OK.", "ping", max_tokens=8)

    def complete(self, system, user, max_tokens=None):
        # JSON mode and deterministic sampling apply only to Ollama.
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0,
                "num_predict": max_tokens or 2048,
            },
        }).encode()

        req = urllib.request.Request(
            self.host + "/api/chat",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())

        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise LLMError(
                "Ollama HTTP %d: %s" % (exc.code, detail)
            ) from exc

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(
                "Could not reach local Ollama at %s: %s. "
                "Is the Ollama app running?" % (self.host, exc)
            ) from exc

        text = data.get("message", {}).get("content", "")

        if not isinstance(text, str) or not text.strip():
            raise LLMError("Ollama returned an empty response")

        self.usage.add(
            data.get("prompt_eval_count", 0),
            data.get("eval_count", 0),
        )
        return text
