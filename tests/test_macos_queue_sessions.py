"""Exercise the real stdio server, daemon and worker across independent sessions."""
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin", "macOS entrypoint default")
class QueueSessionTests(unittest.TestCase):
    def test_start_worker_poll_and_cancel_across_sessions(self):
        asyncio.run(self.scenario())

    async def scenario(self):
        (ROOT / "build").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as tmp:
            root = Path(tmp)
            cli = root / "codex"
            cli.write_text(f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
                           "Path(sys.argv[sys.argv.index('-o')+1]).write_text('queue smoke complete')\n")
            cli.chmod(0o700)
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("HARBOR_") and k not in {"TUNNEL_RUNTIME_KEY", "CONTROL_PLANE_API_KEY"}}
            env.update(HARBOR_USER_SETTINGS_DIR=str(root / "settings"), HARBOR_CODEX_EXE=str(cli),
                       HARBOR_MINIMAX_CLI_EXE=str(root / "missing/mcode"), HARBOR_AGY_EXE=str(root / "missing"),
                       HARBOR_LOG_DIR=str(root / "logs"), HARBOR_TUNNEL_EXE=str(root / "missing"))
            jobs = root / "settings/state/jobs"
            daemon = None
            with (root / "stderr.log").open("w") as errors:
                async with AsyncExitStack() as stack:
                    async def connect(args):
                        read, write = await stack.enter_async_context(stdio_client(
                            StdioServerParameters(command=sys.executable, args=args, env=env, cwd=ROOT), errlog=errors))
                        client = await stack.enter_async_context(ClientSession(read, write))
                        await client.initialize()
                        return client

                    async def call(client, name, args):
                        result = await client.call_tool(name, args)
                        self.assertFalse(result.isError)
                        return result.structuredContent or json.loads(result.content[0].text)

                    # One direct server and one App runtime; neither is given HARBOR_JOBS_DIR.
                    producer = await connect([str(ROOT / "server_legacy.py")])
                    reader = await connect(["-m", "harbor_runtime", "mcp"])
                    args = dict(harness="codex", prompt="fixture", cwd=str(root), sandbox="workspace-write")
                    cancelled = await call(producer, "task_start", args)
                    self.assertTrue(cancelled["ok"], cancelled)
                    self.assertTrue((jobs / cancelled["job_id"] / "status.json").is_file())
                    self.assertTrue((await call(reader, "task_cancel", {"job_id": cancelled["job_id"]}))["ok"])
                    self.assertEqual((await call(producer, "task_poll", {"job_id": cancelled["job_id"]}))["status"], "cancelled")
                    started = await call(producer, "task_start", args)
                    self.assertTrue(started["ok"], started)
                    try:
                        daemon = subprocess.Popen([sys.executable, "-m", "harbor_runtime", "daemon"],
                                                  cwd=ROOT, env=env, stdout=errors, stderr=errors)
                        state_path = jobs / started["job_id"] / "status.json"
                        for _ in range(100):
                            state = json.loads(state_path.read_text())
                            if state["status"] in {"completed", "failed"}:
                                break
                            await asyncio.sleep(0.1)
                        self.assertEqual(state["status"], "completed", state)
                        polled = await call(reader, "task_poll", {"job_id": started["job_id"]})
                        self.assertTrue(polled["ok"], polled)
                        self.assertEqual(polled["final_message"], "queue smoke complete")
                        reopened = await connect(["-m", "harbor_runtime", "mcp"])
                        self.assertEqual((await call(reopened, "task_poll", {"job_id": started["job_id"]}))["status"], "completed")
                    finally:
                        if daemon:
                            daemon.terminate()
                            daemon.wait(timeout=10)
