"""Unit tests for CredentialStore, Windows Credential Manager backend mock, and fail-closed safety."""

from __future__ import annotations

import ctypes
import sys
from unittest.mock import MagicMock

import pytest
import launcher.credential_store as credential_store

from launcher.credential_store import (
    CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY,
    CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY,
    CredentialBackendUnavailableError,
    CredentialOperationError,
    CredentialStore,
    InMemoryCredentialBackend,
    MacOSKeychainBackend,
    WindowsCredentialManagerBackend,
    get_default_backend,
)


@pytest.fixture(autouse=True)
def _cross_platform_ctypes_shims(monkeypatch):
    """Provide Win32-only ctypes hooks and structures for injected fakes."""
    last_error = 0

    def set_last_error(value):
        nonlocal last_error
        last_error = value

    def get_last_error():
        return last_error

    monkeypatch.setattr(ctypes, "set_last_error", set_last_error, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", get_last_error, raising=False)

    if credential_store._CREDENTIALW is None:
        class _TestFileTime(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

        class _TestCredential(ctypes.Structure):
            _fields_ = [
                ("Flags", ctypes.c_uint32),
                ("Type", ctypes.c_uint32),
                ("TargetName", ctypes.c_wchar_p),
                ("Comment", ctypes.c_wchar_p),
                ("LastWritten", _TestFileTime),
                ("CredentialBlobSize", ctypes.c_uint32),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", ctypes.c_uint32),
                ("AttributeCount", ctypes.c_uint32),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", ctypes.c_wchar_p),
                ("UserName", ctypes.c_wchar_p),
            ]

        monkeypatch.setattr(credential_store, "_CREDENTIALW", _TestCredential)
        monkeypatch.setattr(credential_store, "_PCREDENTIALW", ctypes.POINTER(_TestCredential))


# ---------------------------------------------------------------------------
# Stable Target Identity Constants
# ---------------------------------------------------------------------------


def test_credential_target_constants():
    """Target names must be stable, explicit, and namespaced."""
    assert CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY == "Harness-Harbor:tunnel:runtime_key"
    assert CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY == "Harness-Harbor:codex:custom_api_key"


# ---------------------------------------------------------------------------
# In-Memory / Fake Backend API
# ---------------------------------------------------------------------------


def test_in_memory_credential_store_api():
    """Test standard store/read/delete/exists lifecycle using the in-memory fake backend."""
    fake_backend = InMemoryCredentialBackend()
    store = CredentialStore(backend=fake_backend)

    target = CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY
    secret = "sk-tunnel-secret-token-xyz"

    # Initially absent
    assert not store.exists(target)
    assert store.read(target) is None
    assert not store.delete(target)

    # Store
    store.store(target, secret)
    assert store.exists(target)
    assert store.read(target) == secret

    # Delete
    assert store.delete(target) is True
    assert not store.exists(target)
    assert store.read(target) is None
    assert store.delete(target) is False


# ---------------------------------------------------------------------------
# Fully Mocked Windows Backend (Real Credential Manager is untouched)
# ---------------------------------------------------------------------------


class _MockAdvapi32:
    """Mock advapi32.dll Win32 Credential Manager API for unit testing."""

    def __init__(self):
        self.vault: dict[str, bytes] = {}
        self.forced_error = 0
        self.write_calls = 0
        self.read_calls = 0
        self.delete_calls = 0
        self.free_calls = 0
        self.last_write_blob_type = None
        self._allocated_credentials: list[object] = []

    def CredWriteW(self, pcred_ref, flags) -> int:
        self.write_calls += 1
        if self.forced_error != 0:
            ctypes.set_last_error(self.forced_error)
            return 0
        cred = pcred_ref._obj
        target = cred.TargetName
        size = cred.CredentialBlobSize
        self.last_write_blob_type = type(cred.CredentialBlob)
        blob = ctypes.string_at(cred.CredentialBlob, size)
        self.vault[target] = blob
        ctypes.set_last_error(0)
        return 1

    def CredReadW(self, target, cred_type, flags, ppcred) -> int:
        self.read_calls += 1
        if self.forced_error != 0:
            ctypes.set_last_error(self.forced_error)
            return 0
        if target not in self.vault:
            ctypes.set_last_error(1168)  # ERROR_NOT_FOUND
            return 0

        raw_bytes = self.vault[target]
        mock_cred = credential_store._CREDENTIALW()
        mock_cred.TargetName = target
        mock_cred.CredentialBlobSize = len(raw_bytes)
        blob_buffer = (ctypes.c_ubyte * len(raw_bytes)).from_buffer_copy(raw_bytes)
        mock_cred.CredentialBlob = ctypes.cast(
            blob_buffer, ctypes.POINTER(ctypes.c_ubyte)
        )

        # Model CredReadW's heap-allocated structure and retain it until the
        # mock CredFree call.  This avoids using the real credential manager.
        credential_pointer = ctypes.pointer(mock_cred)
        ctypes.cast(
            ppcred, ctypes.POINTER(credential_store._PCREDENTIALW)
        )[0] = credential_pointer
        self._allocated_credentials.append((mock_cred, blob_buffer, credential_pointer))
        ctypes.set_last_error(0)
        return 1

    def CredDeleteW(self, target, cred_type, flags) -> int:
        self.delete_calls += 1
        if self.forced_error != 0:
            ctypes.set_last_error(self.forced_error)
            return 0
        if target in self.vault:
            del self.vault[target]
            ctypes.set_last_error(0)
            return 1
        ctypes.set_last_error(1168)  # ERROR_NOT_FOUND
        return 0

    def CredFree(self, pbuffer) -> None:
        self.free_calls += 1


def test_windows_backend_fully_mocked_lifecycle():
    """Verify Windows backend behaves correctly with a fully mocked advapi32."""
    mock_adv = _MockAdvapi32()
    backend = WindowsCredentialManagerBackend(advapi32=mock_adv)
    store = CredentialStore(backend=backend)

    target = CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY
    secret = "sk-custom-route-key-999"

    # Initially absent
    assert not store.exists(target)
    assert store.read(target) is None
    assert not store.delete(target)

    # Store
    store.store(target, secret)
    assert mock_adv.write_calls == 1
    assert target in mock_adv.vault
    assert mock_adv.vault[target] == secret.encode("utf-8")

    # Read
    read_back = store.read(target)
    assert read_back == secret
    assert mock_adv.free_calls >= 1

    # Exists
    assert store.exists(target) is True

    # Delete
    assert store.delete(target) is True
    assert not store.exists(target)
    assert store.read(target) is None


def test_windows_backend_uses_lpbyte_and_size_bounded_blob_round_trip():
    """A CredentialBlob pointer preserves bytes after an embedded NUL."""
    mock_adv = _MockAdvapi32()
    backend = WindowsCredentialManagerBackend(advapi32=mock_adv)
    target = "test:pointer-size"
    secret = "before\x00after-\N{SNOWMAN}"

    backend.store(target, secret)

    assert mock_adv.last_write_blob_type is ctypes.POINTER(ctypes.c_ubyte)
    assert mock_adv.vault[target] == secret.encode("utf-8")
    assert backend.read(target) == secret


def test_windows_backend_binds_advapi_with_last_error_support(monkeypatch):
    """The native DLL binding must opt into ctypes thread-local last errors."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake_advapi = MagicMock()
    binding = MagicMock()

    def fake_windll(name, *, use_last_error):
        binding(name, use_last_error=use_last_error)
        return fake_advapi

    monkeypatch.setattr(credential_store.ctypes, "WinDLL", fake_windll, raising=False)

    WindowsCredentialManagerBackend()

    binding.assert_called_once_with("Advapi32.dll", use_last_error=True)


def test_windows_backend_fails_closed_on_store_error():
    """OS store failure raises CredentialOperationError and fails closed without secret in message."""
    mock_adv = _MockAdvapi32()
    mock_adv.forced_error = 5  # ERROR_ACCESS_DENIED
    mock_adv.GetLastError = MagicMock(return_value=123)
    backend = WindowsCredentialManagerBackend(advapi32=mock_adv)

    secret = "sk-very-confidential-secret"
    with pytest.raises(CredentialOperationError) as exc_info:
        backend.store("test-target", secret)

    err_msg = str(exc_info.value)
    assert "error 5" in err_msg
    mock_adv.GetLastError.assert_not_called()
    # Crucial security guarantee: secret must NOT appear in error message
    assert secret not in err_msg


def test_windows_backend_fails_closed_on_read_error():
    """OS read failure other than ERROR_NOT_FOUND raises CredentialOperationError."""
    mock_adv = _MockAdvapi32()
    mock_adv.forced_error = 5  # ERROR_ACCESS_DENIED
    mock_adv.GetLastError = MagicMock(return_value=123)
    backend = WindowsCredentialManagerBackend(advapi32=mock_adv)

    with pytest.raises(CredentialOperationError) as exc_info:
        backend.read("test-target")

    assert "read failed" in str(exc_info.value)
    assert "error 5" in str(exc_info.value)
    mock_adv.GetLastError.assert_not_called()


def test_windows_backend_fails_closed_on_delete_error():
    """OS delete failure other than ERROR_NOT_FOUND raises CredentialOperationError."""
    mock_adv = _MockAdvapi32()
    mock_adv.forced_error = 5  # ERROR_ACCESS_DENIED
    mock_adv.GetLastError = MagicMock(return_value=123)
    backend = WindowsCredentialManagerBackend(advapi32=mock_adv)

    with pytest.raises(CredentialOperationError) as exc_info:
        backend.delete("test-target")

    assert "delete failed" in str(exc_info.value)
    assert "error 5" in str(exc_info.value)
    mock_adv.GetLastError.assert_not_called()


# ---------------------------------------------------------------------------
# Platform Fallback / Fail-Closed Guarantees
# ---------------------------------------------------------------------------


def test_unsupported_platform_fails_closed(monkeypatch):
    """Unsupported platform raises CredentialBackendUnavailableError; no plaintext fallback."""
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(CredentialBackendUnavailableError) as exc_info:
        get_default_backend()

    assert "No supported secure credential backend" in str(exc_info.value)
    assert "Plaintext fallback is forbidden" in str(exc_info.value)


class _MockCoreFoundation:
    def __init__(self):
        self.release_calls = []

    def CFRelease(self, item):
        self.release_calls.append(item.value)


class _MockSecurity:
    NOT_FOUND = -25300

    def __init__(self, *, add_status=0):
        self.vault = {}
        self.items = {}
        self.buffers = {}
        self.next_item = 1
        self.add_status = add_status
        self.find_calls = 0
        self.add_calls = 0
        self.modify_calls = 0
        self.delete_calls = 0
        self.free_content_calls = 0

    def _target(self, service_length, service, account_length, account):
        assert service == MacOSKeychainBackend.SERVICE
        assert service_length == len(service)
        return bytes(account[:account_length]).decode("utf-8")

    def _item_for(self, target):
        item = self.items.get(target)
        if item is None:
            item = self.next_item
            self.next_item += 1
            self.items[target] = item
        return item

    def SecKeychainFindGenericPassword(
        self, _keychain, service_length, service, account_length, account,
        password_length, password_data, item_ref
    ):
        self.find_calls += 1
        target = self._target(service_length, service, account_length, account)
        if target not in self.vault:
            return self.NOT_FOUND

        item_id = self._item_for(target)
        item_ref._obj.value = item_id
        if password_length is not None:
            raw = self.vault[target]
            buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
            self.buffers[ctypes.addressof(buffer)] = buffer
            password_length._obj.value = len(raw)
            password_data._obj.value = ctypes.addressof(buffer)
        return 0

    def SecKeychainAddGenericPassword(
        self, _keychain, service_length, service, account_length, account,
        password_length, password, _item_ref
    ):
        self.add_calls += 1
        if self.add_status:
            return self.add_status
        target = self._target(service_length, service, account_length, account)
        self.vault[target] = bytes(password[:password_length])
        self._item_for(target)
        return 0

    def SecKeychainItemModifyAttributesAndData(self, item, _attributes, data_length, data):
        self.modify_calls += 1
        target = next(target for target, item_id in self.items.items() if item_id == item.value)
        self.vault[target] = bytes(data[:data_length])
        return 0

    def SecKeychainItemDelete(self, item):
        self.delete_calls += 1
        target = next(target for target, item_id in self.items.items() if item_id == item.value)
        del self.vault[target]
        return 0

    def SecKeychainItemFreeContent(self, _attributes, data):
        self.free_content_calls += 1
        self.buffers.pop(data.value, None)
        return 0


def test_macos_keychain_backend_fully_mocked_lifecycle():
    """Exercise Keychain lifecycle through injected Security.framework fakes only."""
    security = _MockSecurity()
    core_foundation = _MockCoreFoundation()
    store = CredentialStore(MacOSKeychainBackend(security, core_foundation))
    target = CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY

    assert not store.exists(target)
    assert store.read(target) is None
    assert not store.delete(target)

    store.store(target, "mac-secret")
    assert store.exists(target)
    assert store.read(target) == "mac-secret"

    store.store(target, "updated-secret")
    assert store.read(target) == "updated-secret"
    assert store.delete(target)
    assert not store.exists(target)
    assert not store.delete(target)
    assert security.add_calls == 1
    assert security.modify_calls == 1
    assert security.free_content_calls == 2
    assert core_foundation.release_calls


def test_macos_keychain_backend_fails_closed_on_operation_error():
    """Security.framework failures raise without persisting or exposing secrets."""
    security = _MockSecurity(add_status=-50)
    backend = MacOSKeychainBackend(security, _MockCoreFoundation())
    secret = "mac-confidential-secret"

    with pytest.raises(CredentialOperationError) as exc_info:
        backend.store("target", secret)

    assert "OSStatus -50" in str(exc_info.value)
    assert secret not in str(exc_info.value)
    assert security.vault == {}


def test_macos_keychain_binding_fails_closed_without_real_framework_call(monkeypatch):
    """Binding errors stay fail-closed and never reach a real Keychain library."""
    monkeypatch.setattr(sys, "platform", "darwin")
    cdll = MagicMock(side_effect=OSError("framework unavailable"))
    monkeypatch.setattr(credential_store.ctypes, "CDLL", cdll)

    with pytest.raises(CredentialBackendUnavailableError) as exc_info:
        MacOSKeychainBackend().read("target")

    assert "Plaintext fallback is forbidden" in str(exc_info.value)
    cdll.assert_called_once_with("/System/Library/Frameworks/Security.framework/Security")


# ---------------------------------------------------------------------------
# Secret Safety & Validation
# ---------------------------------------------------------------------------


def test_credential_store_argument_validation():
    """CredentialStore validates non-empty string arguments."""
    store = CredentialStore(backend=InMemoryCredentialBackend())

    with pytest.raises(ValueError):
        store.store("", "secret")

    with pytest.raises(ValueError):
        store.store("target", 12345)  # type: ignore

    with pytest.raises(ValueError):
        store.read("")

    with pytest.raises(ValueError):
        store.delete("")

    with pytest.raises(ValueError):
        store.exists("")


def test_credential_store_repr_is_safe():
    """CredentialStore repr hides all secrets and internal state."""
    fake = InMemoryCredentialBackend()
    fake.store("target", "super-secret-token")
    store = CredentialStore(backend=fake)

    r = repr(store)
    assert "CredentialStore" in r
    assert "InMemoryCredentialBackend" in r
    assert "super-secret-token" not in r
