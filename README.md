# jwswarm-plugin-bitwarden

A prototype WorkSwarm plugin that types saved passwords into the agent's browser **without the password ever reaching the model**.

The agent only writes placeholders such as `{{vault:example.com:password}}`. The plugin swaps in the real value at the last moment, right before the browser tool runs, and swaps it back out afterwards. The long-term goal is a Bitwarden-backed credential broker. For now, secrets come from a plain-text `vault.json` that stands in for the Bitwarden CLI (`bw`).

> **Status: prototype.** It works end to end in the WorkSwarm desktop app, but it isn't ready for real passwords yet. See [Limitations](#limitations).

## How it works

The plugin is a rail (`VaultFillRail`), shipped as a standard WorkSwarm **plugin package** (`plugin/vault-fill/`, with a `rails` entry in `manifest.json`). It passes WorkSwarm's own plugin validator, including the hot-load check. It doesn't modify WorkSwarm itself. The same rail also works as a custom rail extension (`extensions/vault_fill/`), which is how it was first tested.

1. **Reach the browser subagent.** WorkSwarm attaches user rails to the main agent only, but browser tools run inside a separate `browser_agent` subagent. On startup the rail adds a copy of itself to that subagent's config. Subagent configs accept rails and call `fork_for_agent()` on each one.
2. **Tell the agent the placeholders.** A system-prompt section lists the saved sites and their `{{vault:<site>:username}}` / `{{vault:<site>:password}}` placeholders. Usernames are shown; passwords never are.
3. **Fill (`before_tool_call`, runs last).** If a browser tool call contains a placeholder, the rail checks that the current page's host matches the vault entry's site, then swaps in the real value. A mismatch blocks the call, which stops a saved login from being typed into a phishing or prompt-injected page.
4. **Restore (`after_tool_call` / `on_tool_exception`, run first).** The rail puts the placeholder back on the tool call object, which is the same object stored in chat history, and redacts any password from the tool result.
5. **Scrub model requests.** Playwright's page snapshots show the contents of input fields, including password boxes, and WorkSwarm's browser runtime adds them to the model's context after rails run. So the browser subagent's model client is wrapped to replace any vault password with `{{vault:redacted}}` before a request is sent.

## Repository layout

| Path | What it is |
|---|---|
| `plugin/vault-fill/` | The plugin package: `manifest.json`, `rails/vault_fill_rail.py`, `rails/vault.json` (**fake test logins only**, standing in for `bw`) |
| `extensions/vault_fill/` | The same rail as a custom rail extension (the first test setup) |
| `extensions/vault_leak_probe/rail.py` | Test instrument, not part of the plugin. Patches httpx, aiohttp and `Model.invoke`/`stream` process-wide and logs whether a vault password appears in any outbound request |
| `test_site/server.py`, `users.json` | Local login form on port 8765. Issues a random account code on each successful sign-in and logs whether the received password was correct |
| `run_chat.py` | Sends one prompt to a running WorkSwarm through its ACP endpoint; `--plugins vault-fill` enables the plugin for that chat |

## Test results (2026-11-10, WorkSwarm desktop app, deepseek-v4-flash)

Every run asked the agent to sign in to the test site and report the account code. A random code is issued per sign-in, so an answer remembered from an earlier run can't pass.

| Fill path | Signed in, code matched | Requests containing the password |
|---|---|---|
| `browser_fill_form`, swap only (steps 1–4) | yes | **1 leaked** (page snapshot) |
| `browser_batch_interact`, swap only | yes | 0 of 25 |
| `browser_fill_form`, swap + model scrub (steps 1–5) | yes | 0 of 48 |
| Saved login used on the wrong site | blocked | 0 |
| **Plugin package**, `browser_fill_form`, swap + scrub | yes | 0 of 6 model requests, 0 of 75 outbound HTTP |

The first four rows used the extension setup. The last row used the plugin package, enabled for the chat with `plugin_names`.

After the extension runs, a scan of every file WorkSwarm wrote found neither password in plain text.

## Try it

Requirements: the WorkSwarm desktop app, and the WorkSwarm Python environment for `run_chat.py`.

1. Install the plugin: copy `plugin/vault-fill` into `~/.jiuwenswarm/agent/workspace/plugins/plugin_packages/local/`, then register it with WorkSwarm's plugin-creator script:
   ```bash
   python <site-packages>/jiuwenswarm/resources/agent/workspace/skills/plugin-creator/scripts/register_plugin.py vault-fill
   ```
2. Optional, to measure leaks: copy `extensions/vault_leak_probe` into `~/.jiuwenswarm/agent/workspace/extensions/` and enable it in `extensions_config.json` there:
   ```json
   {"vault_leak_probe": {"name": "vault_leak_probe", "class_name": "VaultLeakProbeRail", "enabled": true, "priority": 1}}
   ```
   Then restart WorkSwarm, because extensions are only loaded at startup. Don't enable `extensions/vault_fill` alongside the package, since both define the same rail.
3. Start the test site:
   ```bash
   python test_site/server.py 8765
   ```
4. Start a new chat with the Vault Fill plugin enabled. In the app, tick the plugin. Or, with WorkSwarm's Python:
   ```bash
   python run_chat.py "Open http://localhost:8765/ and sign in with my saved login, then report the account code" --plugins vault-fill
   ```
5. Check the results:
   - `vault_fill/audit.log` under the agent workspace (`~/.jiuwenswarm/agent/workspace/`) records fills, blocks, restores and scrubs. It never contains secrets.
   - `extensions/vault_leak_probe/probe_report.jsonl` has one line per request; `"leak": true` means a password was sent.
   - `test_site/attempts.log` shows whether the site received the right password and which code it issued.

Turn the probe off again (`"enabled": false`) when you're done. It patches network libraries for the whole agent process.

## Limitations

- **No per-use approval yet.** In the wrong-site test the agent, after being blocked, switched on its own to a saved login that did match the page. Each use should be approved by the user through WorkSwarm's permission prompt.
- **Relies on internal behaviour.** The fill works because agent-core copies the rail's edited `tool_args` onto the shared tool-call object, and the scrub works by replacing methods on the model object. Both are internals, so an update could break them. A supported "secret argument" hook in agent-core would be the proper fix.
- **Hub review unknown.** Plugin packages can ship rails, and this one passes the validator and works end to end. How the Hub reviews a published plugin that contains a rail hasn't been tested.
- **No real secret store.** The next step is to replace `vault.json` with the Bitwarden CLI (`bw`) and handle unlocking, with the Bitwarden Agent Access SDK as a later option.
