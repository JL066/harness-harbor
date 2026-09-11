"""Bundle acceptance in a fresh workspace. Live CLI use requires --live-codex."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


async def smoke(runtime, live, harness="codex"):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    (ROOT / "build").mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="macos-acceptance-", dir=ROOT / "build"))
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("HARBOR_") and key not in {"TUNNEL_RUNTIME_KEY", "CONTROL_PLANE_API_KEY", "CONTROL_PLANE_TUNNEL_ID"}}
    env.update(HARBOR_STATE_DIR=str(root / "state"), HARBOR_USER_SETTINGS_DIR=str(root / "settings"),
               HARBOR_LOG_DIR=str(root / "logs"), HARBOR_TUNNEL_PROFILE_DIR=str(root / "tunnel"),
               HARBOR_TUNNEL_EXE=str(root / "not-installed"), HARBOR_AGY_EXE=str(root / "not-installed"),
               HARBOR_MINIMAX_CLI_EXE=str(root / "not-installed/mcode"))
    if live:
        exe = shutil.which(harness)
        if not exe:
            raise RuntimeError(f"{harness} is unavailable")
        env[f"HARBOR_{harness.upper()}_EXE"] = exe
    else:
        env["HARBOR_CODEX_EXE"] = str(root / "not-installed")
    # Deliberately minimal PATH proves the bundle does not need a Python install.
    env["PATH"] = "/usr/bin:/bin"
    with (root / "bridge.stderr.log").open("wb") as errors:
        bridge = subprocess.Popen([str(runtime), "bridge"], cwd=root, env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        def call(method):
            import select
            bridge.stdin.write(json.dumps({"v": 1, "id": "acceptance", "method": method, "params": {}}).encode() + b"\n")
            bridge.stdin.flush()
            if not select.select([bridge.stdout], [], [], 30)[0]:
                raise RuntimeError("Bridge response timeout")
            result = json.loads(bridge.stdout.readline())
            assert result["ok"], result
            return result["result"]
        report = {"runtime": str(runtime), "fixture": str(root), "live_harness": harness if live else None}
        try:
            report["hello"] = call("hello")
            started = call("runtime.start")
            assert started["components"]["mcp"]["state"] == "healthy", started
            assert started["components"]["daemon"]["state"] == "healthy", started
            report["mcp_start"] = True
            if live:
                seed, workspace = root / "seed", root / "worktree"
                seed.mkdir()
                (seed / "total.py").write_text("def total(values):\n    return sum(values) + 1\n")
                (seed / "check.py").write_text("from total import total\nassert total([1, 2, 3]) == 6\nassert total([]) == 0\n")
                def git(*args):
                    subprocess.run(["git", "-C", str(seed), *args], capture_output=True, check=True)
                git("init")
                git("add", "total.py", "check.py")
                git("-c", "user.name=Harbor Acceptance", "-c", "user.email=acceptance@example.invalid", "commit", "-m", "Acceptance fixture")
                git("worktree", "add", "-b", "macos-smoke", str(workspace))
                params = StdioServerParameters(command=str(runtime), args=["mcp"], env=env, cwd=root)
                async with stdio_client(params, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        result = await client.call_tool("task_start", {"harness": harness, "prompt":
                            "Fix total.py so total(values) returns the correct sum including empty input. "
                            "Only edit total.py inside this worktree. Read check.py but do not edit it. "
                            "Do not access external files, credentials, or network. Do not commit. "
                            "Run the check if Python is available and finish with a brief summary.",
                            "cwd": str(workspace), "model": "gpt-5.5" if harness == "codex" else None, "sandbox": "workspace-write", "route": "current"})
                        task = result.structuredContent or json.loads(result.content[0].text)
                        assert task.get("ok"), task
                        job_id = task["job_id"]
                        state_path = root / "state/jobs" / job_id / "status.json"
                        end = time.monotonic() + 240
                        while time.monotonic() < end:
                            state = json.loads(state_path.read_text())
                            if state.get("status") in {"completed", "failed", "cancelled"}:
                                break
                            await asyncio.sleep(2)
                        # One task_poll after completion; no bypass of poll throttling.
                        polled = await client.call_tool("task_poll", {"job_id": job_id})
                        final = polled.structuredContent or json.loads(polled.content[0].text)
                        report["job_id"] = job_id
                        report["final_status"] = final.get("status")
                        assert final.get("status") == "completed", "Live task did not complete; inspect isolated fixture state"
                        subprocess.run([sys.executable, "check.py"], cwd=workspace, check=True)
                        report["independent_check"] = True
                call("runtime.restart")
                async with stdio_client(params, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        polled = await client.call_tool("task_poll", {"job_id": job_id})
                        final = polled.structuredContent or json.loads(polled.content[0].text)
                        assert final.get("status") == "completed"
                report["persisted_after_restart"] = True
            else:
                call("runtime.restart")
            report["restart"] = True
            stopped = call("shutdown")
            assert stopped["state"] == "stopped"
            bridge.wait(timeout=10)
            report["shutdown"] = True
        finally:
            bridge.stdin.close()
            try:
                bridge.wait(timeout=10)
            except subprocess.TimeoutExpired:
                bridge.terminate()
                bridge.wait(timeout=10)
            bridge.stdout.close()
            (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", type=Path)
    live_group = parser.add_mutually_exclusive_group()
    live_group.add_argument("--live-codex", action="store_true")
    live_group.add_argument("--live-agy", action="store_true")
    args = parser.parse_args()
    asyncio.run(smoke(args.runtime.resolve(), args.live_codex or args.live_agy, "agy" if args.live_agy else "codex"))
