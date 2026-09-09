"""Compatibility import surface for managed tunnel lifecycle support."""
from launcher.tunnel import (
    ManagedTunnelSupervisor,
    TestConnectionResult,
    TunnelCredentialError,
    test_connection,
)

__all__ = ["ManagedTunnelSupervisor", "TestConnectionResult", "TunnelCredentialError", "test_connection"]
