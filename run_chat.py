"""Send one prompt to the running WorkSwarm backend over its ACP endpoint.

Usage (WorkSwarm venv python):
    python run_chat.py "<prompt>" [out_file] [--plugins name1,name2]

Requires the backend to be running (WorkSwarm desktop app or jiuwenswarm-start).
Permission prompts are auto-approved by AcpStdioClient (ACP_AUTO_APPROVE_PERMISSIONS
default true). ``--plugins`` enables installed plugin packages for this chat: the
gateway forwards session/prompt params into chat.send, which reads ``plugin_names``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

from jiuwenswarm.acp.stdio_client import AcpStdioClient


def _inject_plugins(client: AcpStdioClient, plugins: list[str]) -> None:
    """Add plugin_names to every session/prompt request the client writes."""
    stdin = client._proc.stdin  # noqa: SLF001 - test helper
    original_write = stdin.write

    def write(data: bytes):
        try:
            payload = json.loads(data.decode("utf-8"))
            if payload.get("method") == "session/prompt":
                payload["params"]["plugin_names"] = plugins
                data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        except Exception:  # noqa: BLE001 - not a JSON line; pass through
            pass
        return original_write(data)

    stdin.write = write


async def main(prompt: str, out_file: str | None, plugins: list[str]) -> int:
    command = shutil.which("jiuwenswarm-acp") or str(Path(sys.executable).parent / "jiuwenswarm-acp.exe")
    client = AcpStdioClient(command, [])
    try:
        await client.connect()
        if plugins:
            _inject_plugins(client, plugins)
        reply = await client.chat(prompt, timeout=900)
    finally:
        try:
            await asyncio.wait_for(client.close(), timeout=30)
        except Exception:  # noqa: BLE001
            pass
    print("=== AGENT REPLY ===")
    print(reply)
    if out_file:
        Path(out_file).write_text(reply or "", encoding="utf-8")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("out_file", nargs="?")
    parser.add_argument("--plugins", default="", help="comma-separated plugin package ids")
    args = parser.parse_args()
    plugin_list = [p.strip() for p in args.plugins.split(",") if p.strip()]
    sys.exit(asyncio.run(main(args.prompt, args.out_file, plugin_list)))
