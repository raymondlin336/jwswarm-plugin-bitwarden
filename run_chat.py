"""Send one prompt to the running WorkSwarm backend over its ACP endpoint.

Usage (WorkSwarm venv python): python run_chat.py "<prompt>" [out_file]
Requires the backend to be running (jiuwenswarm-start). Permission prompts
are auto-approved by AcpStdioClient (ACP_AUTO_APPROVE_PERMISSIONS default true).
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

from jiuwenswarm.acp.stdio_client import AcpStdioClient


async def main(prompt: str, out_file: str | None) -> int:
    command = shutil.which("jiuwenswarm-acp") or str(Path(sys.executable).parent / "jiuwenswarm-acp.exe")
    client = AcpStdioClient(command, [])
    try:
        await client.connect()
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
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)))
