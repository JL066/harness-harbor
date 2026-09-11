"""Unit tests for Harness Harbor brand assets and multi-scale icons."""

import hashlib
import struct
from pathlib import Path
from PIL import Image

from launcher.config import (
    BRAND_DIR,
    BRAND_HEADER_ICON,
    BRAND_ICO_PATH,
    BRAND_PNG_DIR,
    BRAND_SYSTEM_ICO_PATH,
    BRAND_TRAY_ICON,
)
from launcher.tools.build_brand_assets import CANONICAL_MASTERS, ICO_SIZES, SOURCE_DIR


def test_canonical_source_masters_hash_and_dimensions():
    """Verify source masters remain immutable with exact expected SHA256 hashes."""
    for key, spec in CANONICAL_MASTERS.items():
        p = SOURCE_DIR / spec["file"]
        assert p.exists(), f"Source master {spec['file']} must exist"
        data = p.read_bytes()
        actual_hash = hashlib.sha256(data).hexdigest().upper()
        assert actual_hash == spec["sha256"].upper(), f"Hash mismatch for {spec['file']}"
        with Image.open(p) as im:
            assert im.size == (1024, 1024), f"Master {spec['file']} must be 1024x1024"


def test_generated_png_assets_exist_and_valid():
    """Verify all required generated scene PNG assets exist with correct dimensions and PNG magic."""
    expected_pngs = {
        "harbor-1024.png": (1024, 1024),
        "harbor-512.png": (512, 512),
        "harbor-256.png": (256, 256),
        "harbor-128.png": (128, 128),
        "harbor-64.png": (64, 64),
        "harbor-48.png": (48, 48),
        "harbor-40.png": (40, 40),
        "harbor-32-compact.png": (32, 32),
        "harbor-32-tiny.png": (32, 32),
        "harbor-32.png": (32, 32),
        "harbor-24.png": (24, 24),
        "harbor-20.png": (20, 20),
        "harbor-16.png": (16, 16),
        "harbor-header-rounded.png": (128, 128),
    }

    for name, expected_size in expected_pngs.items():
        p = BRAND_PNG_DIR / name
        assert p.exists(), f"Generated asset {name} must exist"

        # Check PNG magic bytes
        raw_header = p.read_bytes()[:8]
        assert raw_header == b"\x89PNG\r\n\x1a\n", f"{name} must have valid PNG magic bytes"

        # Check dimensions and readability with Pillow
        with Image.open(p) as im:
            assert im.size == expected_size, f"{name} dimension mismatch: expected {expected_size}, got {im.size}"
            assert im.format == "PNG", f"{name} format must be PNG"


def test_generated_system_png_assets_exist_and_transparent():
    """Verify all transparent system-level PNG assets exist and have RGBA alpha channel."""
    system_sizes = [1024, 256, 128, 64, 48, 40, 32, 24, 20, 16]
    for sz in system_sizes:
        name = f"harbor-system-{sz}.png"
        p = BRAND_PNG_DIR / name
        assert p.exists(), f"System asset {name} must exist"

        raw_header = p.read_bytes()[:8]
        assert raw_header == b"\x89PNG\r\n\x1a\n", f"{name} must have valid PNG magic bytes"

        with Image.open(p) as im:
            assert im.size == (sz, sz), f"{name} dimension mismatch: expected {(sz, sz)}, got {im.size}"
            assert im.mode == "RGBA", f"{name} must be RGBA with alpha channel"
            # Verify transparency is present
            alpha = im.split()[3]
            min_a, max_a = alpha.getextrema()
            assert min_a == 0, f"{name} must contain transparent pixels (min alpha=0)"
            assert max_a > 0, f"{name} must contain visible pixels (max alpha>0)"


def test_harbor_system_ico_embedded_resolutions():
    """Verify multi-resolution harbor-system.ico and harbor.ico have all 9 embedded sizes."""
    for ico_path in [BRAND_SYSTEM_ICO_PATH, BRAND_ICO_PATH]:
        assert ico_path.exists(), f"{ico_path.name} must exist"

        raw = ico_path.read_bytes()
        assert len(raw) >= 6, "ICO file too short"

        reserved, ico_type, count = struct.unpack("<HHH", raw[:6])
        assert reserved == 0, "ICO reserved field must be 0"
        assert ico_type == 1, "ICO type must be 1"
        assert count == len(ICO_SIZES), f"Expected {len(ICO_SIZES)} images, got {count}"

        embedded_sizes = []
        offset = 6
        for _ in range(count):
            entry = raw[offset : offset + 16]
            w = entry[0] or 256
            h = entry[1] or 256
            embedded_sizes.append((w, h))
            offset += 16

        expected_sizes = [(sz, sz) for sz in ICO_SIZES]
        assert sorted(embedded_sizes) == sorted(expected_sizes), (
            f"Embedded sizes mismatch in {ico_path.name}: expected {expected_sizes}, got {embedded_sizes}"
        )


def test_config_brand_asset_paths():
    """Verify configuration references valid existing asset paths."""
    assert BRAND_HEADER_ICON.exists(), f"Header icon {BRAND_HEADER_ICON} must exist"
    assert BRAND_TRAY_ICON.exists(), f"Tray icon {BRAND_TRAY_ICON} must exist"
    assert BRAND_SYSTEM_ICO_PATH.exists(), f"System ICO path {BRAND_SYSTEM_ICO_PATH} must exist"
    assert BRAND_ICO_PATH.exists(), f"ICO path {BRAND_ICO_PATH} must exist"
