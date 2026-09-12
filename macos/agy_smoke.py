"""Explicit live AGY acceptance through Harbor MCP and its isolated queue daemon."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


async def smoke():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    if sys.platform != "darwin":
        raise RuntimeError("This acceptance checks the macOS AGY permission mapping")
    executable = shutil.which("agy")
    if not executable:
        raise RuntimeError("AGY CLI is not installed")
    (ROOT / "build").mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="agy-write-smoke-", dir=ROOT / "build"))
    workspace = root / "workspace"
    workspace.mkdir()
    settings = Path.home() / ".gemini/antigravity-cli/settings.json"
    def settings_digest():
        return hashlib.sha256(settings.read_bytes()).hexdigest() if settings.exists() else None
    before_settings = settings_digest()
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARBOR_")}
    env.update(HARBOR_STATE_DIR=str(root / "state"), HARBOR_JOBS_DIR=str(root / "state/jobs"),
               HARBOR_USER_SETTINGS_DIR=str(root / "settings"), HARBOR_LOG_DIR=str(root / "logs"),
               HARBOR_TUNNEL_PROFILE_DIR=str(root / "tunnel"), HARBOR_AGY_EXE=executable,
               HARBOR_AGY_DANGEROUSLY_SKIP_PERMISSIONS="0")
    report = {"fixture": str(root), "checks": {}}
    with (root / "runtime.stderr.log").open("w") as errors:
        daemon = subprocess.Popen([sys.executable, "-m", "harbor_runtime", "daemon"],
                                  cwd=ROOT, env=env, stdout=errors, stderr=errors)
        try:
            params = StdioServerParameters(command=sys.executable, args=["-m", "harbor_runtime", "mcp"], env=env, cwd=str(ROOT))
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    response = await client.call_tool("task_start", {
                        "harness": "agy", "model": "gemini-3.8-flash-medium", "reasoning_effort": "medium",
                        "sandbox": "workspace-write", "cwd": str(workspace),
                        "prompt": "Create agy_write_smoke.txt in the task workspace with exactly HARNESS_HARBOR_AGY_WRITE_OK followed by a newline. Use the direct file writing tool only. Do not run commands, delegate, or write any other file. Reply briefly when done.",
                    })
                    task = response.structuredContent or json.loads(response.content[0].text)
                    assert task.get("ok"), task
                    report["job_id"] = task["job_id"]
                    report["checks"]["task_start"] = True
                    state_path = root / "state/jobs" / task["job_id"] / "status.json"
                    deadline = time.monotonic() + 180
                    while time.monotonic() < deadline:
                        state = json.loads(state_path.read_text())
                        if state["status"] in {"completed", "failed", "cancelled"}:
                            break
                        await asyncio.sleep(1)
                    else:
                        raise RuntimeError("AGY smoke timed out; inspect preserved isolated state")
                    response = await client.call_tool("task_poll", {"job_id": task["job_id"]})
                    final = response.structuredContent or json.loads(response.content[0].text)
                    target = workspace / "agy_write_smoke.txt"
                    report["checks"].update(
                        completed=final.get("status") == "completed",
                        file_created=target.is_file(),
                        exact_content=target.is_file() and target.read_text() == "HARNESS_HARBOR_AGY_WRITE_OK\n",
                        no_denied_actions=not state.get("denied_actions"),
                        no_dangerous_bypass=state.get("agy_dangerously_skip_permissions") is False,
                        only_expected_workspace_output=sorted(p.name for p in workspace.iterdir()) == ["agy_write_smoke.txt"],
                        global_settings_unchanged=settings_digest() == before_settings,
                    )
                    assert all(report["checks"].values()), report
        finally:
            daemon.terminate()
            daemon.wait(timeout=15)
            (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True, help="Run a real AGY task using the existing login")
    parser.parse_args()
    asyncio.run(smoke())
