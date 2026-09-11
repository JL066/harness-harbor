"""Secure credential store abstraction and OS-protected backends.

This module provides a secure CredentialStore interface for storing sensitive
tokens (such as Tunnel Runtime Key and Codex Custom Route API Key) using the
underlying OS credential vault (Windows Credential Manager or macOS Keychain).

Security guarantees:
- Secrets are NEVER persisted in plaintext files or registry keys.
- Fails closed: if the OS vault is unavailable or encounters an error, an
  exception is raised rather than falling back to unencrypted storage.
- Permanent environment variables are never altered.
- Secret values are never exposed in exception messages or repr strings.
- macOS calls Security.framework directly without shelling out with secrets.
"""

from __future__ import annotations

import ctypes
from abc import ABC, abstractmethod
from typing import Any
import sys

# ---------------------------------------------------------------------------
# Stable namespaced credential identities
# ---------------------------------------------------------------------------

CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY = "Harness-Harbor:tunnel:runtime_key"
CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY = "Harness-Harbor:codex:custom_api_key"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CredentialStoreError(Exception):
    """Base exception for all credential store operations."""


class CredentialBackendUnavailableError(CredentialStoreError):
    """Raised when no secure credential backend is available on the current OS."""


class CredentialOperationError(CredentialStoreError):
    """Raised when an OS-level credential vault operation fails."""


# ---------------------------------------------------------------------------
# Backend Abstraction
# ---------------------------------------------------------------------------


class BaseCredentialBackend(ABC):
    """Abstract base class for OS-protected credential vault backends."""

    @abstractmethod
    def store(self, target: str, secret: str) -> None:
        """Store a secret for the given target identity.

        Parameters
        ----------
        target:
            Unique namespaced target identifier.
        secret:
            The sensitive credential value to store.

        Raises
        ------
        CredentialStoreError:
            If the vault operation fails.
        """

    @abstractmethod
    def read(self, target: str) -> str | None:
        """Read a secret for the given target identity.

        Parameters
        ----------
        target:
            Unique namespaced target identifier.

        Returns
        -------
        str | None:
            The stored credential string, or ``None`` if not found.

        Raises
        ------
        CredentialStoreError:
            If the vault operation fails.
        """

    @abstractmethod
    def delete(self, target: str) -> bool:
        """Delete a secret for the given target identity.

        Parameters
        ----------
        target:
            Unique namespaced target identifier.

        Returns
        -------
        bool:
            ``True`` if the credential was deleted, ``False`` if it did not exist.

        Raises
        ------
        CredentialStoreError:
            If the vault operation fails.
        """

    @abstractmethod
    def exists(self, target: str) -> bool:
        """Check if a credential exists for the given target identity.

        Parameters
        ----------
        target:
            Unique namespaced target identifier.

        Returns
        -------
        bool:
            ``True`` if present, ``False`` otherwise.

        Raises
        ------
        CredentialStoreError:
            If the vault operation fails.
        """


# ---------------------------------------------------------------------------
# Windows Credential Manager Backend (advapi32.dll)
# ---------------------------------------------------------------------------

# Win32 Constants
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168

if sys.platform == "win32":
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", _FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            # LPBYTE, not c_char_p: ctypes auto-converts c_char_p fields to
            # Python bytes and loses the native pointer needed for a
            # CredentialBlobSize-bounded read.
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)
else:
    _CREDENTIALW = None  # type: ignore
    _PCREDENTIALW = None  # type: ignore


class WindowsCredentialManagerBackend(BaseCredentialBackend):
    """Windows Credential Manager backend using advapi32.dll Win32 APIs."""

    def __init__(self, advapi32: Any = None) -> None:
        """Initialize backend, optionally accepting an advapi32 mock for testing."""
        if advapi32 is not None:
            self._advapi32 = advapi32
        else:
            if sys.platform != "win32":
                raise CredentialBackendUnavailableError(
                    "WindowsCredentialManagerBackend requires a Windows platform."
                )
            try:
                # use_last_error=True preserves the Win32 error set by these
                # functions in ctypes' thread-local last-error slot.
                adv = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
                from ctypes import wintypes

                adv.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
                adv.CredWriteW.restype = wintypes.BOOL
                adv.CredReadW.argtypes = [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(_PCREDENTIALW),
                ]
                adv.CredReadW.restype = wintypes.BOOL
                adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
                adv.CredDeleteW.restype = wintypes.BOOL
                adv.CredFree.argtypes = [ctypes.c_void_p]
                adv.CredFree.restype = None
                self._advapi32 = adv
            except Exception as exc:
                raise CredentialBackendUnavailableError(
                    f"Failed to bind Windows Credential Manager APIs: {exc}"
                ) from exc

    def _get_last_error(self) -> int:
        return int(ctypes.get_last_error())

    def store(self, target: str, secret: str) -> None:
        blob = secret.encode("utf-8")
        blob_buffer = None
        cred = _CREDENTIALW()
        cred.Flags = 0
        cred.Type = _CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.Comment = "Harness Harbor Managed Credential"
        cred.CredentialBlobSize = len(blob)
        if blob:
            blob_buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
            cred.CredentialBlob = ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_ubyte))
        else:
            cred.CredentialBlob = None
        cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = "Harness Harbor"

        # Keep blob_buffer alive until CredWriteW has returned.
        ret = self._advapi32.CredWriteW(ctypes.byref(cred), 0)
        if not ret:
            err = self._get_last_error()
            raise CredentialOperationError(
                f"Windows Credential Manager store failed for target '{target}' (win32 error {err})."
            )

    def read(self, target: str) -> str | None:
        pcred = _PCREDENTIALW()
        ret = self._advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred))
        if not ret:
            err = self._get_last_error()
            if err == _ERROR_NOT_FOUND:
                return None
            raise CredentialOperationError(
                f"Windows Credential Manager read failed for target '{target}' (win32 error {err})."
            )

        try:
            size = pcred.contents.CredentialBlobSize
            blob_ptr = pcred.contents.CredentialBlob
            if size > 0 and blob_ptr:
                raw = ctypes.string_at(blob_ptr, size)
                return raw.decode("utf-8", errors="replace")
            return ""
        finally:
            self._advapi32.CredFree(pcred)

    def delete(self, target: str) -> bool:
        ret = self._advapi32.CredDeleteW(target, _CRED_TYPE_GENERIC, 0)
        if not ret:
            err = self._get_last_error()
            if err == _ERROR_NOT_FOUND:
                return False
            raise CredentialOperationError(
                f"Windows Credential Manager delete failed for target '{target}' (win32 error {err})."
            )
        return True

    def exists(self, target: str) -> bool:
        pcred = _PCREDENTIALW()
        ret = self._advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred))
        if not ret:
            err = self._get_last_error()
            if err == _ERROR_NOT_FOUND:
                return False
            raise CredentialOperationError(
                f"Windows Credential Manager check failed for target '{target}' (win32 error {err})."
            )
        try:
            return True
        finally:
            self._advapi32.CredFree(pcred)


# ---------------------------------------------------------------------------
# macOS Keychain Backend Stub (Future batch extension)
# ---------------------------------------------------------------------------


class MacOSKeychainBackend(BaseCredentialBackend):
    """Native Security.framework calls; no shell, argv secret, or disk fallback.

    The Swift host owns user interaction. This compatibility backend shares its
    service/account namespace with the existing CredentialStore callers.
    """

    SERVICE = b"com.jl066.harness-harbor"

    def __init__(self, security=None, core_foundation=None):
        self._security = security
        self._cf = core_foundation

    def _bind(self):
        if self._security is not None:
            return
        if sys.platform != "darwin":
            raise CredentialBackendUnavailableError("macOS Keychain is unavailable. Plaintext fallback is forbidden.")
        try:
            lib = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
            cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            ptr, uint = ctypes.c_void_p, ctypes.c_uint32
            signatures = {
                "SecKeychainFindGenericPassword": [ptr, uint, ctypes.c_char_p, uint, ctypes.c_char_p, ctypes.POINTER(uint), ctypes.POINTER(ptr), ctypes.POINTER(ptr)],
                "SecKeychainAddGenericPassword": [ptr, uint, ctypes.c_char_p, uint, ctypes.c_char_p, uint, ctypes.c_char_p, ctypes.POINTER(ptr)],
                "SecKeychainItemModifyAttributesAndData": [ptr, ptr, uint, ctypes.c_char_p],
                "SecKeychainItemDelete": [ptr], "SecKeychainItemFreeContent": [ptr, ptr],
            }
            for name, args in signatures.items():
                function = getattr(lib, name)
                function.argtypes = args
                function.restype = ctypes.c_int32
            cf.CFRelease.argtypes = [ptr]
            cf.CFRelease.restype = None
            self._security, self._cf = lib, cf
        except Exception:
            raise CredentialBackendUnavailableError("Cannot bind macOS Keychain. Plaintext fallback is forbidden.") from None

    @staticmethod
    def _check(status):
        if status:
            raise CredentialOperationError(f"Keychain operation failed (OSStatus {int(status)}).")

    def _find(self, target, content=False):
        self._bind()
        account = target.encode("utf-8")
        size, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None, len(self.SERVICE), self.SERVICE, len(account), account,
            ctypes.byref(size) if content else None, ctypes.byref(data) if content else None, ctypes.byref(item))
        if status == -25300:  # errSecItemNotFound
            return None, None
        self._check(status)
        try:
            value = ctypes.string_at(data, size.value).decode("utf-8") if content else None
        except Exception:
            if item.value:
                self._cf.CFRelease(item)
            raise CredentialOperationError("Keychain credential cannot be decoded.") from None
        finally:
            if data.value:
                self._security.SecKeychainItemFreeContent(None, data)
        return item, value

    def store(self, target: str, secret: str) -> None:
        item, _ = self._find(target)
        blob = secret.encode("utf-8")
        try:
            if item is not None:
                status = self._security.SecKeychainItemModifyAttributesAndData(item, None, len(blob), blob)
            else:
                account = target.encode("utf-8")
                status = self._security.SecKeychainAddGenericPassword(None, len(self.SERVICE), self.SERVICE,
                    len(account), account, len(blob), blob, None)
            self._check(status)
        finally:
            if item is not None:
                self._cf.CFRelease(item)

    def read(self, target: str) -> str | None:
        item, value = self._find(target, content=True)
        if item is not None:
            self._cf.CFRelease(item)
        return value

    def delete(self, target: str) -> bool:
        item, _ = self._find(target)
        if item is None:
            return False
        try:
            self._check(self._security.SecKeychainItemDelete(item))
            return True
        finally:
            self._cf.CFRelease(item)

    def exists(self, target: str) -> bool:
        item, _ = self._find(target)
        if item is None:
            return False
        self._cf.CFRelease(item)
        return True


# ---------------------------------------------------------------------------
# In-Memory Backend (Testing & Fake)
# ---------------------------------------------------------------------------


class InMemoryCredentialBackend(BaseCredentialBackend):
    """In-memory credential store for tests and isolated environments."""

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    def store(self, target: str, secret: str) -> None:
        self._secrets[target] = secret

    def read(self, target: str) -> str | None:
        return self._secrets.get(target)

    def delete(self, target: str) -> bool:
        if target in self._secrets:
            del self._secrets[target]
            return True
        return False

    def exists(self, target: str) -> bool:
        return target in self._secrets


# ---------------------------------------------------------------------------
# Default Backend Selector
# ---------------------------------------------------------------------------


def get_default_backend() -> BaseCredentialBackend:
    """Select the appropriate secure backend for the current operating system.

    Raises
    ------
    CredentialBackendUnavailableError:
        If no secure vault is supported on the current platform.
    """
    if sys.platform == "win32":
        return WindowsCredentialManagerBackend()
    elif sys.platform == "darwin":
        return MacOSKeychainBackend()
    else:
        raise CredentialBackendUnavailableError(
            f"No supported secure credential backend for platform: {sys.platform}. "
            "Plaintext fallback is forbidden."
        )


# ---------------------------------------------------------------------------
# High-Level CredentialStore
# ---------------------------------------------------------------------------


class CredentialStore:
    """Secure credential store for managing sensitive tokens without plaintext fallback.

    This class provides high-level store/read/delete/exists operations backed
    by an OS-protected credential vault.

    Parameters
    ----------
    backend:
        Optional backend instance. If omitted, the platform default is selected.
    """

    def __init__(self, backend: BaseCredentialBackend | None = None) -> None:
        self._backend = backend if backend is not None else get_default_backend()

    @property
    def backend(self) -> BaseCredentialBackend:
        """The underlying credential backend in use."""
        return self._backend

    def store(self, target: str, secret: str) -> None:
        """Securely store a secret credential.

        Parameters
        ----------
        target:
            Unique namespaced target name.
        secret:
            Sensitive secret string to store.
        """
        if not target or not isinstance(target, str):
            raise ValueError("Credential target must be a non-empty string.")
        if not isinstance(secret, str):
            raise ValueError("Credential secret must be a string.")
        self._backend.store(target, secret)

    def read(self, target: str) -> str | None:
        """Read a secret credential.

        Parameters
        ----------
        target:
            Unique namespaced target name.

        Returns
        -------
        str | None:
            The stored secret string, or ``None`` if not found.
        """
        if not target or not isinstance(target, str):
            raise ValueError("Credential target must be a non-empty string.")
        return self._backend.read(target)

    def delete(self, target: str) -> bool:
        """Delete a secret credential.

        Parameters
        ----------
        target:
            Unique namespaced target name.

        Returns
        -------
        bool:
            ``True`` if removed, ``False`` if absent.
        """
        if not target or not isinstance(target, str):
            raise ValueError("Credential target must be a non-empty string.")
        return self._backend.delete(target)

    def exists(self, target: str) -> bool:
        """Check whether a secret credential exists.

        Parameters
        ----------
        target:
            Unique namespaced target name.

        Returns
        -------
        bool:
            ``True`` if present, ``False`` otherwise.
        """
        if not target or not isinstance(target, str):
            raise ValueError("Credential target must be a non-empty string.")
        return self._backend.exists(target)

    def __repr__(self) -> str:
        # Secret safety: never expose credentials or internal state
        return f"<CredentialStore backend={self._backend.__class__.__name__}>"


__all__ = [
    "BaseCredentialBackend",
    "CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY",
    "CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY",
    "CredentialBackendUnavailableError",
    "CredentialOperationError",
    "CredentialStore",
    "CredentialStoreError",
    "InMemoryCredentialBackend",
    "MacOSKeychainBackend",
    "WindowsCredentialManagerBackend",
    "get_default_backend",
]
