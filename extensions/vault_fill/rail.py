"""Vault fill rail: types saved passwords into the browser without the model seeing them.

Prototype for the Bitwarden credential-broker plugin. Secrets come from a
plain-text vault.json next to this file (stand-in for the `bw` CLI).

How it works
------------
* The model only ever writes placeholders such as ``{{vault:localhost:8765:password}}``.
* WorkSwarm loads this rail on the main agent. In ``init`` it attaches a copy of
  itself to the ``browser_agent`` subagent config (subagent specs accept rails and
  call ``fork_for_agent()`` on each one), because browser tools run in that subagent.
* ``before_tool_call`` runs LAST (lowest priority): it checks the current page is on
  the vault entry's site, then swaps the placeholder for the real value in the
  arguments about to be executed.
* ``after_tool_call`` / ``on_tool_exception`` run FIRST (highest priority): they put
  the placeholder back on the tool call object (which is the same object stored in
  chat history) and redact any password from the tool result.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger("vault_fill")

HERE = Path(__file__).resolve().parent
VAULT_FILE = HERE / "vault.json"  # read-only; package files are never written


def _audit_file() -> Path | None:
    """Audit log under the current agent's workspace (plugin packages are read-only)."""
    try:
        from openjiuwen.core.sys_operation.cwd import get_workspace

        workspace = get_workspace()
    except Exception:  # noqa: BLE001
        workspace = None
    if not workspace:
        return None
    path = Path(workspace) / "vault_fill" / "audit.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

PLACEHOLDER_RE = re.compile(r"\{\{vault:([^{}]+?):(username|password)\}\}")
URL_RE = re.compile(r"Page URL:\s*(https?://[^\s\"'<>)\]]+)", re.IGNORECASE)
REDACTED = "{{vault:redacted}}"
SECTION_NAME = "vault_fill_saved_logins"


def _load_vault() -> dict[str, dict[str, str]]:
    try:
        return json.loads(VAULT_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.error("[vault_fill] cannot read vault: %s", exc)
        return {}


def _audit(event: str, **fields: Any) -> None:
    """Append a non-secret audit line."""
    try:
        audit_file = _audit_file()
        if audit_file is None:
            return
        with audit_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%H:%M:%S"), "event": event, **fields}) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _host_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""


def _map_strings(value: Any, fn) -> Any:
    """Apply fn to every string inside nested dict/list values."""
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {k: _map_strings(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_map_strings(v, fn) for v in value]
    if isinstance(value, tuple):
        return tuple(_map_strings(v, fn) for v in value)
    return value


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(value)


class VaultFillRail(DeepAgentRail):
    """Fills {{vault:site:field}} placeholders in browser tool calls; the model never sees saved passwords."""

    priority: int = 50

    def __init__(self, role: str = "main") -> None:
        super().__init__()
        self.role = role
        self.vault = _load_vault()
        self.current_url: str | None = None
        self._prompt_builder = None
        self._agent = None
        # id(ctx) -> (original tool_args, original tool_call.arguments)
        self._pending: dict[int, tuple[Any, Any]] = {}

    # ---- wiring ---------------------------------------------------------------

    def fork_for_agent(self) -> "VaultFillRail":
        """Called by DeepAgent.create_subagent for each rail in a subagent spec."""
        return VaultFillRail(role="browser")

    def callback_priority(self, event: AgentCallbackEvent) -> int:
        if event == AgentCallbackEvent.BEFORE_TOOL_CALL:
            return -10_000  # run last: other rails only ever see the placeholder
        if event in (AgentCallbackEvent.AFTER_TOOL_CALL, AgentCallbackEvent.ON_TOOL_EXCEPTION):
            return 10_000  # run first: restore before anything else reads it
        return self.priority

    def init(self, agent) -> None:
        self._agent = agent
        self._prompt_builder = getattr(agent, "system_prompt_builder", None)
        if self.role == "main":
            self._attach_to_browser_subagent(agent)
        else:
            self._wrap_model(agent)
        _audit("init", role=self.role, sites=sorted(self.vault))

    # ---- last line of defence: scrub every request this agent sends ----------

    def _secret_variants(self) -> list[tuple[str, str]]:
        pairs = []
        for entry in self.vault.values():
            secret = entry.get("password")
            if secret:
                for v in {secret, json.dumps(secret)[1:-1]}:
                    pairs.append((v, REDACTED))
        return pairs

    def _scrub_messages(self, messages: Any) -> tuple[Any, int]:
        """Return messages with vault passwords replaced, plus how many messages changed."""
        pairs = self._secret_variants()

        def scrub(s: str) -> str:
            for secret, repl in pairs:
                s = s.replace(secret, repl)
            return s

        def has_secret(value: Any) -> bool:
            text = _as_text(value) if not hasattr(value, "model_dump") else _as_text(value.model_dump())
            return any(secret in text for secret, _ in pairs)

        if isinstance(messages, str):
            new = scrub(messages)
            return new, int(new != messages)
        if not isinstance(messages, list):
            return messages, 0
        changed = 0
        out = []
        for m in messages:
            if not has_secret(m):
                out.append(m)
                continue
            changed += 1
            if isinstance(m, dict):
                out.append(_map_strings(m, scrub))
            elif hasattr(m, "model_dump") and hasattr(m, "model_validate"):
                out.append(type(m).model_validate(_map_strings(m.model_dump(), scrub)))
            else:
                out.append(m)
        return out, changed

    def _wrap_model(self, agent) -> None:
        deep_config = getattr(agent, "deep_config", None) or getattr(agent, "_deep_config", None)
        model = getattr(deep_config, "model", None)
        if model is None or getattr(model, "_vault_fill_wrapped", False):
            _audit("model_wrap_skipped", reason="no model" if model is None else "already wrapped")
            return
        rail = self
        orig_invoke = model.invoke
        orig_stream = model.stream

        async def invoke(messages, *args, **kwargs):
            messages, n = rail._scrub_messages(messages)
            if n:
                _audit("scrubbed_model_input", messages=n, via="invoke")
            return await orig_invoke(messages, *args, **kwargs)

        async def stream(messages, *args, **kwargs):
            messages, n = rail._scrub_messages(messages)
            if n:
                _audit("scrubbed_model_input", messages=n, via="stream")
            async for chunk in orig_stream(messages, *args, **kwargs):
                yield chunk

        try:
            model.invoke = invoke
            model.stream = stream
            model._vault_fill_wrapped = True
            _audit("model_wrapped", model_type=type(model).__name__)
        except Exception as exc:  # noqa: BLE001
            _audit("model_wrap_failed", error=str(exc))

    def uninit(self, agent) -> None:
        if self._prompt_builder is not None:
            try:
                self._prompt_builder.remove_section(SECTION_NAME)
            except Exception:  # noqa: BLE001
                pass

    def _attach_to_browser_subagent(self, agent) -> bool:
        deep_config = getattr(agent, "deep_config", None) or getattr(agent, "_deep_config", None)
        specs = getattr(deep_config, "subagents", None) or []
        for spec in specs:
            card = getattr(spec, "agent_card", None)
            if getattr(card, "name", None) != "browser_agent":
                continue
            rails = list(getattr(spec, "rails", None) or [])
            if any(type(r).__name__ == "VaultFillRail" for r in rails):
                return True
            rails.append(VaultFillRail(role="browser"))
            try:
                spec.rails = rails
            except Exception as exc:  # noqa: BLE001
                _audit("attach_failed", error=str(exc))
                return False
            _audit("attached_to_browser_agent")
            return True
        _audit("browser_agent_not_found", subagents=[
            getattr(getattr(s, "agent_card", None), "name", "?") for s in specs
        ])
        return False

    # ---- prompt ---------------------------------------------------------------

    def _prompt_text(self) -> str:
        lines = [
            "## Saved logins (vault)",
            "The user has saved logins for these sites. You never see or handle the passwords.",
        ]
        for site, entry in self.vault.items():
            lines.append(
                f"- {site} (username {entry.get('username', '?')}): username placeholder "
                f"`{{{{vault:{site}:username}}}}`, password placeholder `{{{{vault:{site}:password}}}}`"
            )
        if self.role == "main":
            lines.append(
                "To sign in to one of these sites, delegate to browser_agent and tell it to type the "
                "placeholders above, exactly as written, into the username and password fields. "
                "Never ask the user for a password and never try to reveal it."
            )
        else:
            lines.append(
                "To sign in, type the placeholder text exactly as written into the field "
                "(browser_type or browser_fill_form). The system replaces it with the real value "
                "only when the current page is on that site. Never try to read the value back."
            )
        return "\n".join(lines)

    async def before_model_call(self, ctx) -> None:
        if self._prompt_builder is None:
            return
        try:
            from openjiuwen.harness.prompts import PromptSection

            self._prompt_builder.add_section(
                PromptSection(name=SECTION_NAME, content={"en": self._prompt_text(),
                                                          "cn": self._prompt_text()}, priority=85)
            )
        except Exception as exc:  # noqa: BLE001
            _audit("prompt_section_failed", error=str(exc))
            self._prompt_builder = None

    async def before_invoke(self, ctx) -> None:
        # Config reloads can rebuild subagent specs; re-attach if needed.
        if self.role == "main" and self._agent is not None:
            self._attach_to_browser_subagent(self._agent)

    # ---- tool hooks -----------------------------------------------------------

    def _track_url(self, tool_name: str, args: Any, result_text: str = "") -> None:
        if "navigate" in tool_name and isinstance(args, (dict, str)):
            parsed = args
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                except Exception:  # noqa: BLE001
                    parsed = {}
            url = parsed.get("url") if isinstance(parsed, dict) else None
            if isinstance(url, str) and url.startswith("http"):
                self.current_url = url
        found = URL_RE.findall(result_text or "")
        if found:
            self.current_url = found[-1]

    def _block(self, ctx, reason: str) -> None:
        from openjiuwen.core.foundation.llm import ToolMessage

        tool_call = ctx.inputs.tool_call
        call_id = str(getattr(tool_call, "id", "") or "")
        message = f"[vault] Blocked: {reason}"
        ctx.inputs.tool_result = message
        ctx.inputs.tool_msg = ToolMessage(content=message, tool_call_id=call_id)
        skips = ctx.extra.setdefault("_skip_tool_calls", {})
        skips[call_id] = True
        _audit("blocked", tool=ctx.inputs.tool_name, reason=reason)

    async def before_tool_call(self, ctx) -> None:
        inputs = ctx.inputs
        args = getattr(inputs, "tool_args", None)
        if args is None:
            return
        text = _as_text(args)
        if "{{vault:" not in text:
            return
        if self.role == "main":
            # The main agent has no browser tools; never resolve outside the browser.
            return

        page_host = _host_of(self.current_url)
        for site, field in PLACEHOLDER_RE.findall(text):
            if site not in self.vault:
                self._block(ctx, f"no saved login for '{site}'.")
                return
            if page_host != site.lower():
                self._block(ctx, f"the saved login for '{site}' can only be used on that site; "
                                 f"the current page is '{page_host or 'unknown'}'.")
                return

        # Prototype: no approval prompt yet (the real plugin would ask the user here).
        def fill(s: str) -> str:
            return PLACEHOLDER_RE.sub(lambda m: self.vault[m.group(1)][m.group(2)], s)

        if isinstance(args, str):
            try:
                new_args: Any = json.dumps(_map_strings(json.loads(args), fill), ensure_ascii=False)
            except Exception:  # noqa: BLE001
                new_args = PLACEHOLDER_RE.sub(
                    lambda m: json.dumps(self.vault[m.group(1)][m.group(2)])[1:-1], args)
        else:
            new_args = _map_strings(copy.deepcopy(args), fill)

        tool_call = getattr(inputs, "tool_call", None)
        self._pending[id(ctx)] = (args, getattr(tool_call, "arguments", None))
        inputs.tool_args = new_args
        _audit("filled", tool=inputs.tool_name, page=page_host,
               fields=[f"{s}:{f}" for s, f in PLACEHOLDER_RE.findall(text)])

    def _restore(self, ctx) -> None:
        pending = self._pending.pop(id(ctx), None)
        if pending is None:
            return
        original_args, original_call_args = pending
        ctx.inputs.tool_args = original_args
        tool_call = getattr(ctx.inputs, "tool_call", None)
        if tool_call is not None and original_call_args is not None:
            tool_call.arguments = original_call_args
        _audit("restored", tool=ctx.inputs.tool_name)

    def _redact(self, value: Any) -> Any:
        secrets = [e.get("password", "") for e in self.vault.values() if e.get("password")]

        def scrub(s: str) -> str:
            for secret in secrets:
                s = s.replace(secret, REDACTED)
            return s

        if isinstance(value, (str, dict, list, tuple)):
            return _map_strings(value, scrub)
        dumped = _as_text(value)
        if any(secret in dumped for secret in secrets):
            return scrub(dumped)
        return value

    def _redact_outputs(self, ctx) -> None:
        inputs = ctx.inputs
        if inputs.tool_result is not None:
            inputs.tool_result = self._redact(inputs.tool_result)
        msg = inputs.tool_msg
        if msg is not None and isinstance(getattr(msg, "content", None), str):
            msg.content = self._redact(msg.content)

    async def after_tool_call(self, ctx) -> None:
        self._restore(ctx)
        self._redact_outputs(ctx)
        msg = getattr(ctx.inputs, "tool_msg", None)
        self._track_url(ctx.inputs.tool_name or "", ctx.inputs.tool_args,
                        _as_text(getattr(msg, "content", "")) + _as_text(ctx.inputs.tool_result))

    async def on_tool_exception(self, ctx) -> None:
        self._restore(ctx)
        self._redact_outputs(ctx)
