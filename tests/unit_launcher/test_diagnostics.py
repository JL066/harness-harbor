"""Unit tests for Diagnostics and secret redaction."""

from launcher.diagnostics import format_diagnostics_markdown, redact_secrets


def test_redact_secrets_flags():
    cmd = 'tunnel-client.exe --api-key sk-test123456 --profile harness-harbor --token secret_token_xyz'
    redacted = redact_secrets(cmd)
    assert "sk-test123456" not in redacted
    assert "secret_token_xyz" not in redacted
    assert "--api-key <redacted>" in redacted
    assert "--token <redacted>" in redacted


def test_redact_secrets_env():
    cmd = 'OPENAI_API_KEY=sk-super-secret-key-1234 python server_legacy.py'
    redacted = redact_secrets(cmd)
    assert "sk-super-secret-key-1234" not in redacted
    assert "OPENAI_API_KEY=<redacted>" in redacted


def test_redact_bearer_token():
    header = 'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'
    redacted = redact_secrets(header)
    assert "eyJhbGci" not in redacted
    assert "Authorization: Bearer <redacted>" in redacted


def test_redact_env_tunnel_runtime_key():
    text = 'control_plane: api_key: "env:TUNNEL_RUNTIME_KEY"'
    redacted = redact_secrets(text)
    assert "protected" in redacted


def test_format_diagnostics_markdown_no_secrets():
    diag = {
        "timestamp": "2026-09-03 21:00:00",
        "production_path": "C:\\Users\\Example\\HarnessHarbor",
        "git_commit": "abc1234",
        "git_branch": "master",
        "tunnel_executable": "C:\\Users\\Example\\HarnessHarbor\\tools\\tunnel-client.exe",
        "tunnel_executable_exists": True,
        "tunnel_profile_path": "C:\\Users\\Example\\AppData\\Roaming\\tunnel-client\\harness-harbor.yaml",
        "tunnel_profile_exists": True,
        "tunnel_health_url": "http://127.0.0.1:51260",
        "tunnel_probe_status": "HTTP 200 (OK)",
        "tunnel_probe_latency": "1.2ms",
        "tunnel_pids": [1234],
        "tunnel_status": "Healthy",
        "mcp_script": "server_legacy.py",
        "mcp_pids": [2345],
        "mcp_status": "Healthy",
        "daemon_script": "codex_job_daemon.py",
        "daemon_pids": [3456],
        "daemon_status": "Running",
        "python_executable": "C:\\Users\\Example\\HarnessHarbor\\.venv\\Scripts\\python.exe",
        "python_executable_exists": True,
        "overall_status": "All systems healthy",
        "os_platform": "win32",
        "python_version": "3.11.9",
        "jobs_dir": "C:\\Users\\Example\\HarnessHarbor\\.jobs",
        "source": "environment",
        "canonical_path": "c:\\users\\example\\harnessharbor\\.jobs",
        "fingerprint": "0123456789abcdef",
    }
    md = format_diagnostics_markdown(diag)
    assert "# Harness Harbor Diagnostics" in md
    assert "abc1234" in md
    assert "server_legacy.py" in md
    assert "never server.py" in md
    assert "0123456789abcdef" in md
    assert "Queue Config Error" in md


def test_redact_secrets_json():
    text = '{"api_key": "sk-topsecret123", "profile_name": "custom", "token": "tok-456"}'
    redacted = redact_secrets(text)
    assert "sk-topsecret123" not in redacted
    assert "tok-456" not in redacted
    assert '"api_key": "<redacted>"' in redacted
    assert '"token": "<redacted>"' in redacted
    assert '"profile_name": "custom"' in redacted
