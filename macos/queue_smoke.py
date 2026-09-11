"""Opt-in real CLI acceptance: three harnesses, independent MCP sessions, isolated jobs."""
import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def run(runtime):
    from harbor_runtime.config import discover_executables
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    root = Path(tempfile.mkdtemp(prefix="queue-live-", dir=ROOT / "build"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARBOR_")
           and k not in {"TUNNEL_RUNTIME_KEY", "CONTROL_PLANE_API_KEY", "CONTROL_PLANE_TUNNEL_ID"}}
    env.update(HARBOR_USER_SETTINGS_DIR=str(root / "settings"), HARBOR_STATE_DIR=str(root / "state"),
               HARBOR_LOG_DIR=str(root / "logs"), HARBOR_TUNNEL_PROFILE_DIR=str(root / "tunnel"),
               HARBOR_TUNNEL_EXE=str(root / "missing"))
    found = discover_executables()
    for name, key in (("codex", "HARBOR_CODEX_EXE"), ("minimax", "HARBOR_MINIMAX_CLI_EXE"), ("agy", "HARBOR_AGY_EXE")):
        assert found[name], f"Missing {name} CLI"
        env[key] = found[name]
    # Finder-like PATH: the runtime must restore Node/CLI executable paths.
    env["PATH"] = "/usr/bin:/bin"
    report = {"runtime": str(runtime), "fixture": str(root), "jobs": {}}
    daemon = None
    with (root / "stderr.log").open("w") as errors:
        try:
            async with AsyncExitStack() as stack:
                async def connect():
                    read, write = await stack.enter_async_context(stdio_client(StdioServerParameters(
                        command=str(runtime), args=["mcp"], env=env, cwd=root), errlog=errors))
                    client = await stack.enter_async_context(ClientSession(read, write))
                    await client.initialize()
                    return client

                async def call(client, name, args):
                    result = await client.call_tool(name, args)
                    assert not result.isError, name + " MCP error"
                    return result.structuredContent or json.loads(result.content[0].text)

                producer, reader = await connect(), await connect()
                for harness in ("codex", "minimax", "agy"):
                    cwd = root / harness
                    cwd.mkdir()
                    subprocess.run(["git", "init", str(cwd)], capture_output=True, check=True)
                    started = await call(producer, "task_start", {
                        "harness": harness, "cwd": str(cwd), "sandbox": "workspace-write",
                        "model": "gpt-5.5" if harness == "codex" else "gemini-3.8-flash-low" if harness == "agy" else None,
                        "prompt": "This is a connectivity smoke test. Reply exactly HARBOR_QUEUE_SMOKE_OK. "
                                  "Do not call tools, read or write files, run commands, or delegate."})
                    assert started.get("ok"), harness + " start rejected"
                    report["jobs"][harness] = {"job_id": started["job_id"], "queue_root": started.get("queue_root")}
                daemon = subprocess.Popen([str(runtime), "daemon"], cwd=root, env=env, stdout=errors, stderr=errors)
                end = time.monotonic() + 240
                while time.monotonic() < end:
                    states = {name: json.loads((root / "state/jobs" / row["job_id"] / "status.json").read_text())
                              for name, row in report["jobs"].items()}
                    if all(s.get("status") in {"completed", "failed", "cancelled"} for s in states.values()):
                        break
                    await asyncio.sleep(1)
                reopened = await connect()
                for name, row in report["jobs"].items():
                    final = await call(reader, "task_poll", {"job_id": row["job_id"]})
                    row.update(status=final.get("status"), failure_type=final.get("failure_type"),
                               marker_present="HARBOR_QUEUE_SMOKE_OK" in (final.get("final_message") or ""))
                    again = await call(reopened, "task_poll", {"job_id": row["job_id"]})
                    row["cross_session_consistent"] = again.get("ok") is True and again.get("status") == row["status"]
                report["pass"] = all(row["status"] == "completed" and row["marker_present"]
                                     and row["cross_session_consistent"] for row in report["jobs"].values())
        finally:
            if daemon:
                daemon.terminate()
                daemon.wait(timeout=15)
            (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))
    return report.get("pass", False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--live", action="store_true", required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.runtime.resolve())) else 1)
