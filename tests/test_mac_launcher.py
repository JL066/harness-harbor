import os
import tempfile
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import control_plane


class MacStartupTests(unittest.TestCase):
    @unittest.skipIf(control_plane.IS_WINDOWS, "POSIX launcher")
    def test_launcher_syntax(self):
        subprocess.run(["sh", "-n", str(control_plane.PROJECT_ROOT / "start-harbor.sh")], check=True)

    def test_agy_models_probe_allows_network_latency(self):
        with patch.object(control_plane, "run_safe_subprocess") as run:
            for args, expected in [(["models"], 30.0), (["--version"], 5.0), (["--help"], 5.0)]:
                control_plane._run_agy_probe(["agy", *args])
                self.assertEqual(run.call_args.kwargs["timeout"], expected)

    @unittest.skipIf(control_plane.IS_WINDOWS, "POSIX launcher")
    def test_tunnel_uses_profile_without_inherited_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "start-harbor.sh"
            launcher.write_text((control_plane.PROJECT_ROOT / "start-harbor.sh").read_text())
            stub = root / "tunnel-stub"
            stub.write_text('#!/bin/sh\nprintf "%s:%s" "${CONTROL_PLANE_TUNNEL_ID-unset}" "${CONTROL_PLANE_API_KEY-unset}"\n')
            stub.chmod(0o700)
            env = dict(os.environ, HARBOR_TUNNEL_EXE=str(stub),
                       CONTROL_PLANE_TUNNEL_ID="old-tunnel", CONTROL_PLANE_API_KEY="old-key")
            result = subprocess.run(["sh", str(launcher), "tunnel"], env=env,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, "unset:unset")
