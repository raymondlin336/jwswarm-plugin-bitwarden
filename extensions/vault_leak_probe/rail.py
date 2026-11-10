"""Leak probe: test instrument for the vault_fill prototype (not part of the plugin).

Wraps, process-wide, every outbound HTTP request made with httpx or aiohttp and
every openjiuwen Model.invoke/stream call. For each one it records whether any
vault password appears in what is sent (and, for model calls, in what comes
back). Requests to localhost / 127.0.0.1 are logged separately, because the
Playwright MCP server on localhost is supposed to receive the password.

Results: probe_report.jsonl next to this file. Never writes the secret itself.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail

HERE = Path(__file__).resolve().parent
REPORT = HERE / "probe_report.jsonl"
VAULT_FILE = HERE.parent / "vault_fill" / "vault.json"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

_PATCHED = False


def _secrets() -> list[str]:
    try:
        vault = json.loads(VAULT_FILE.read_text(encoding="utf-8"))
        return [e["password"] for e in vault.values() if e.get("password")]
    except Exception:  # noqa: BLE001
        return []


def _variants(secret: str) -> list[str]:
    """The secret as it may appear raw, JSON-escaped, or URL/form-encoded."""
    from urllib.parse import quote, quote_plus

    out = {secret, json.dumps(secret)[1:-1], quote(secret, safe=""), quote_plus(secret)}
    return [v for v in out if v]


def _masked_snippets(text: str, secrets: list[str], width: int = 160) -> list[str]:
    """Context around each hit, with the secret itself masked."""
    out = []
    for secret in secrets:
        for v in _variants(secret):
            start = 0
            while (idx := text.find(v, start)) != -1 and len(out) < 6:
                chunk = text[max(0, idx - width): idx + len(v) + width]
                for s in secrets:
                    for sv in _variants(s):
                        chunk = chunk.replace(sv, "<<PASSWORD>>")
                out.append(chunk)
                start = idx + len(v)
    return out


def _record(kind: str, target: str, text: str, messages: Any = None, **extra: Any) -> None:
    hits = []
    secrets = _secrets()
    for i, secret in enumerate(secrets):
        if any(v in text for v in _variants(secret)):
            hits.append(f"vault_password_{i}")
    if hits:
        extra["snippets"] = _masked_snippets(text, secrets)
        if isinstance(messages, list):
            where = []
            for idx, m in enumerate(messages):
                mt = _to_text(m)
                if any(v in mt for s in secrets for v in _variants(s)):
                    role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else "?")
                    where.append({"index": idx, "of": len(messages), "role": role,
                                  "type": type(m).__name__})
            extra["messages_with_secret"] = where
    try:
        with REPORT.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%H:%M:%S"),
                "probe": "v2",
                "kind": kind,
                "target": target,
                "bytes": len(text),
                "leak": bool(hits),
                "which": hits,
                **extra,
            }) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=lambda o: getattr(o, "__dict__", str(o)))
    except Exception:  # noqa: BLE001
        return str(value)


def _patch_httpx() -> None:
    import httpx

    orig_async = httpx.AsyncClient.send
    orig_sync = httpx.Client.send

    def _scan(request) -> None:
        host = request.url.host or ""
        kind = "http_local" if host in LOCAL_HOSTS else "http_external"
        try:
            body = request.content
        except Exception:  # noqa: BLE001 - streaming body not yet read
            body = b""
        _record(kind, f"{request.method} {host}{request.url.path}", _to_text(body)
                + _to_text(str(request.url)))

    async def send_async(self, request, *args, **kwargs):
        _scan(request)
        return await orig_async(self, request, *args, **kwargs)

    def send_sync(self, request, *args, **kwargs):
        _scan(request)
        return orig_sync(self, request, *args, **kwargs)

    httpx.AsyncClient.send = send_async
    httpx.Client.send = send_sync


def _patch_aiohttp() -> None:
    try:
        import aiohttp
    except Exception:  # noqa: BLE001
        return
    orig = aiohttp.ClientSession._request

    async def request(self, method, str_or_url, *args, **kwargs):
        url = str(str_or_url)
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        kind = "http_local" if host in LOCAL_HOSTS else "http_external"
        payload = _to_text(kwargs.get("json")) + _to_text(kwargs.get("data")) + url
        _record(kind, f"{method} {host}{urlparse(url).path}", payload)
        return await orig(self, method, str_or_url, *args, **kwargs)

    aiohttp.ClientSession._request = request


def _patch_model() -> None:
    from openjiuwen.core.foundation.llm.model import Model

    orig_invoke = Model.invoke
    orig_stream = Model.stream

    async def invoke(self, messages, *args, **kwargs):
        sent = _to_text(messages) + _to_text(kwargs.get("tools"))
        _record("model_input", "invoke", sent, messages=messages)
        result = await orig_invoke(self, messages, *args, **kwargs)
        _record("model_output", "invoke", _to_text(result))
        return result

    async def stream(self, messages, *args, **kwargs):
        sent = _to_text(messages) + _to_text(kwargs.get("tools"))
        _record("model_input", "stream", sent, messages=messages)
        chunks = []
        async for chunk in orig_stream(self, messages, *args, **kwargs):
            chunks.append(_to_text(chunk))
            yield chunk
        _record("model_output", "stream", "".join(chunks))

    Model.invoke = invoke
    Model.stream = stream


def _install() -> None:
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True
    for patch in (_patch_httpx, _patch_aiohttp, _patch_model):
        try:
            patch()
        except Exception as exc:  # noqa: BLE001
            _record("probe_error", patch.__name__, "", error=str(exc))
    _record("probe_installed", "process", "")


class VaultLeakProbeRail(DeepAgentRail):
    """Test instrument: records whether vault passwords appear in any outbound request or model call."""

    priority: int = 1

    def init(self, agent) -> None:
        _install()
