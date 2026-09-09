"""Deterministic build script for Harness Harbor brand icon assets.

Generates multi-scale PNG assets, Header rounded brand mark, and Windows multi-resolution ICO
from canonical brand masters using Lanczos downsampling.
Strictly preserves source master immutability and verifies SHA256 hashes.
"""

from __future__ import annotations

import hashlib
import io
import struct
import sys
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont, ImageOps

# Paths
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BRAND_DIR = REPO_ROOT / "launcher" / "assets" / "brand"
SOURCE_DIR = BRAND_DIR / "source"
GENERATED_DIR = BRAND_DIR / "generated"
PNG_DIR = GENERATED_DIR / "png"
ICO_DIR = GENERATED_DIR / "ico"

# Canonical source masters and expected SHA256 hashes
CANONICAL_MASTERS = {
    "full": {
        "file": "harbor-full-master-1024.jpg",
        "sha256": "57217861AC6B27B4E8F05FA4F0EFCEECE1BA47FB9513B1B7FA832D60BDC01A7F",
        "sizes": [1024, 512, 256, 128],
    },
    "compact": {
        "file": "harbor-compact-master-1024.jpg",
        "sha256": "F14EA036A4126A08FC2866272588CAF94E5F002061D787B7F2C8E4E7EF422454",
        "sizes": [64, 48, 40],
    },
    "tiny": {
        "file": "harbor-tiny-master-1024.jpg",
        "sha256": "DD9DA4033A826D9E0372B546CD0E8BCE0DBC0D634B7A0123BFAC919043CE6FA8",
        "sizes": [24, 20, 16],
    },
    "system": {
        "file": "harbor-system-master-1024.png",
        "sha256": "9D1217D18B77EDF6968EE3583C6D3C982FF254EA17C16442E046711E8D3D538B",
        "sizes": [1024, 256, 128, 64, 48, 40, 32, 24, 20, 16],
    },
}

ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def verify_source_hashes() -> None:
    """Verify that all canonical source masters exist and match expected SHA256 hashes."""
    print("Verifying canonical brand masters...")
    for key, spec in CANONICAL_MASTERS.items():
        master_path = SOURCE_DIR / spec["file"]
        if not master_path.exists():
            raise FileNotFoundError(f"Missing canonical master: {master_path}")

        data = master_path.read_bytes()
        actual_hash = hashlib.sha256(data).hexdigest().upper()
        expected_hash = spec["sha256"].upper()

        if actual_hash != expected_hash:
            raise ValueError(
                f"SHA256 mismatch for {spec['file']}!\n"
                f"  Expected: {expected_hash}\n"
                f"  Actual:   {actual_hash}"
            )

        with Image.open(master_path) as im:
            if im.size != (1024, 1024):
                raise ValueError(f"Invalid dimensions for {spec['file']}: expected 1024x1024, got {im.size}")

        print(f"  [OK] {spec['file']} ({len(data)} bytes, SHA256: {actual_hash[:16]}...)")
    print("All canonical source masters verified successfully.\n")


def render_resized_png(source_im: Image.Image, size: int) -> Image.Image:
    """Deterministic downsampling using Lanczos filter without distortion."""
    if source_im.size == (size, size):
        return source_im.copy()
    return source_im.resize((size, size), resample=Image.Resampling.LANCZOS)


def create_rounded_icon(source_im: Image.Image, size: int, corner_radius: int) -> Image.Image:
    """Apply supersampled anti-aliased squircle rounded corners to an image."""
    base = render_resized_png(source_im, size)
    scale = 4
    mask = Image.new("L", (size * scale, size * scale), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        [0, 0, size * scale - 1, size * scale - 1],
        radius=corner_radius * scale,
        fill=255,
    )
    mask = mask.resize((size, size), Image.Resampling.LANCZOS)
    rounded = base.convert("RGBA").copy()
    rounded.putalpha(mask)
    return rounded


def generate_png_assets() -> dict[str, Image.Image]:
    """Generate all target PNG assets into generated/png."""
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    generated_images: dict[str, Image.Image] = {}

    # Load and normalize source images
    loaded_sources: dict[str, Image.Image] = {}
    for key, spec in CANONICAL_MASTERS.items():
        master_path = SOURCE_DIR / spec["file"]
        raw_im = Image.open(master_path)
        im = ImageOps.exif_transpose(raw_im).convert("RGBA")
        loaded_sources[key] = im

    print("Generating standard multi-scale scene PNG assets...")

    # 1. Full Master: 1024, 512, 256, 128
    for sz in CANONICAL_MASTERS["full"]["sizes"]:
        out_name = f"harbor-{sz}.png"
        out_path = PNG_DIR / out_name
        im_resized = render_resized_png(loaded_sources["full"], sz)
        im_resized.save(out_path, format="PNG", optimize=True)
        generated_images[out_name] = im_resized
        print(f"  [GEN] {out_name} ({sz}x{sz}, from Full Master)")

    # 2. Compact Master: 64, 48, 40
    for sz in CANONICAL_MASTERS["compact"]["sizes"]:
        out_name = f"harbor-{sz}.png"
        out_path = PNG_DIR / out_name
        im_resized = render_resized_png(loaded_sources["compact"], sz)
        im_resized.save(out_path, format="PNG", optimize=True)
        generated_images[out_name] = im_resized
        print(f"  [GEN] {out_name} ({sz}x{sz}, from Compact Master)")

    # 3. 32px A/B Candidates
    im_32_compact = render_resized_png(loaded_sources["compact"], 32)
    im_32_compact.save(PNG_DIR / "harbor-32-compact.png", format="PNG", optimize=True)
    generated_images["harbor-32-compact.png"] = im_32_compact

    im_32_tiny = render_resized_png(loaded_sources["tiny"], 32)
    im_32_tiny.save(PNG_DIR / "harbor-32-tiny.png", format="PNG", optimize=True)
    generated_images["harbor-32-tiny.png"] = im_32_tiny

    # Official scene 32px
    im_32_tiny.save(PNG_DIR / "harbor-32.png", format="PNG", optimize=True)
    generated_images["harbor-32.png"] = im_32_tiny
    print("  [GEN] harbor-32-compact.png & harbor-32-tiny.png & harbor-32.png")

    # 4. Tiny Master: 24, 20, 16
    for sz in CANONICAL_MASTERS["tiny"]["sizes"]:
        out_name = f"harbor-{sz}.png"
        out_path = PNG_DIR / out_name
        im_resized = render_resized_png(loaded_sources["tiny"], sz)
        im_resized.save(out_path, format="PNG", optimize=True)
        generated_images[out_name] = im_resized
        print(f"  [GEN] {out_name} ({sz}x{sz}, from Tiny Master)")

    print("\nGenerating transparent system-level icon PNG assets...")
    # 5. System Master (Transparent Background): 1024, 256, 128, 64, 48, 40, 32, 24, 20, 16
    for sz in CANONICAL_MASTERS["system"]["sizes"]:
        out_name = f"harbor-system-{sz}.png"
        out_path = PNG_DIR / out_name
        im_resized = render_resized_png(loaded_sources["system"], sz)
        im_resized.save(out_path, format="PNG", optimize=True)
        generated_images[out_name] = im_resized
        print(f"  [GEN] {out_name} ({sz}x{sz}, transparent background)")

    # 6. Header Rounded Brand Mark (128x128 squircle anti-aliased from Full Master)
    header_rounded = create_rounded_icon(loaded_sources["full"], 128, corner_radius=28)
    header_rounded_path = PNG_DIR / "harbor-header-rounded.png"
    header_rounded.save(header_rounded_path, format="PNG", optimize=True)
    generated_images["harbor-header-rounded.png"] = header_rounded
    print("  [GEN] harbor-header-rounded.png (128x128 anti-aliased squircle mark for Header)")

    print(f"\nPNG asset generation complete. Total files in {PNG_DIR}: {len(list(PNG_DIR.glob('*.png')))}\n")
    return generated_images


def build_system_ico() -> tuple[Path, Path]:
    """Build multi-resolution Windows harbor-system.ico and harbor.ico from transparent system assets."""
    ICO_DIR.mkdir(parents=True, exist_ok=True)
    system_ico_path = ICO_DIR / "harbor-system.ico"
    harbor_ico_path = ICO_DIR / "harbor.ico"

    # Load system images for all ICO sizes
    images_by_size = {
        sz: Image.open(PNG_DIR / f"harbor-system-{sz}.png").convert("RGBA")
        for sz in ICO_SIZES
    }

    base_im = images_by_size[256]
    append_list = [images_by_size[sz] for sz in [16, 20, 24, 32, 40, 48, 64, 128]]
    target_sizes = [(sz, sz) for sz in ICO_SIZES]

    # Save harbor-system.ico
    base_im.save(
        system_ico_path,
        format="ICO",
        sizes=target_sizes,
        append_images=append_list,
    )
    print(f"Generated multi-resolution system ICO at: {system_ico_path}")
    verify_ico(system_ico_path)

    # Also update harbor.ico with the system icon for transparent shell consistency
    base_im.save(
        harbor_ico_path,
        format="ICO",
        sizes=target_sizes,
        append_images=append_list,
    )
    print(f"Updated multi-resolution harbor.ico at: {harbor_ico_path}")
    verify_ico(harbor_ico_path)

    return system_ico_path, harbor_ico_path


def verify_ico(ico_path: Path) -> list[tuple[int, int]]:
    """Parse binary ICO header and verify all embedded directory entries."""
    raw = ico_path.read_bytes()
    if len(raw) < 6:
        raise ValueError("ICO file too small to be valid")

    reserved, ico_type, count = struct.unpack("<HHH", raw[:6])
    if reserved != 0 or ico_type != 1:
        raise ValueError(f"Invalid ICO magic/type: reserved={reserved}, type={ico_type}")

    embedded_sizes: list[tuple[int, int]] = []
    offset = 6
    for i in range(count):
        entry = raw[offset : offset + 16]
        w = entry[0] or 256
        h = entry[1] or 256
        embedded_sizes.append((w, h))
        offset += 16

    expected_sizes = [(sz, sz) for sz in ICO_SIZES]
    if sorted(embedded_sizes) != sorted(expected_sizes):
        raise ValueError(f"ICO sizes mismatch! Expected {expected_sizes}, got {embedded_sizes}")

    print(f"ICO verification passed ({ico_path.name}): {count} embedded sizes: {embedded_sizes}")
    return embedded_sizes


def create_system_comparison_sheet(out_path: Path) -> None:
    """Generate visual inspection sheet showing system icons across sizes and light/dark modes."""
    sizes = [16, 20, 24, 32, 40, 48, 64]
    sheet = Image.new("RGBA", (780, 280), (242, 242, 247, 255))
    draw = ImageDraw.Draw(sheet)

    # Light taskbar simulation (top)
    draw.rectangle([0, 0, 780, 130], fill=(245, 245, 247, 255))
    draw.text((16, 12), "Light Mode (Windows Light Taskbar / Titlebar):", fill=(40, 40, 45, 255))

    # Dark taskbar simulation (bottom)
    draw.rectangle([0, 130, 780, 280], fill=(32, 32, 34, 255))
    draw.text((16, 142), "Dark Mode (Windows Dark Taskbar / Titlebar):", fill=(230, 230, 235, 255))

    x = 24
    for sz in sizes:
        p = PNG_DIR / f"harbor-system-{sz}.png"
        im = Image.open(p).convert("RGBA")

        # Light row
        draw.text((x, 38), f"{sz}px", fill=(100, 100, 105, 255))
        sheet.paste(im, (x, 56), im)

        # Dark row
        draw.text((x, 170), f"{sz}px", fill=(180, 180, 185, 255))
        sheet.paste(im, (x, 188), im)

        x += sz + 38

    sheet.save(out_path, format="PNG")
    print(f"System icon comparison sheet generated at: {out_path}")


def main():
    print("=== Harness Harbor Brand & System Asset Build ===")
    verify_source_hashes()
    generate_png_assets()

    # Build system ICO and update harbor.ico
    build_system_ico()

    # Create inspection sheets
    create_system_comparison_sheet(BRAND_DIR / "comparison_system_icons.png")

    # Post-build source immutability check
    print("\nVerifying source immutability after build...")
    verify_source_hashes()
    print("=== All brand and system icon assets built successfully! ===")


if __name__ == "__main__":
    main()
