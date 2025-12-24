from __future__ import annotations

import sys
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageFilter, ImageOps


def _has_alpha(img: Image.Image) -> bool:
    return img.mode in ("LA", "RGBA") or (img.mode == "P" and "transparency" in img.info)


def _trim_transparent(img: Image.Image, alpha_threshold: int = 8) -> Image.Image:
    if not _has_alpha(img):
        return img
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    alpha = img.split()[-1]
    # Make a binary mask at threshold to avoid cutting soft edges too aggressively.
    mask = alpha.point(lambda a: 255 if a > alpha_threshold else 0, mode="L")
    bbox = mask.getbbox()
    if not bbox:
        return img
    return img.crop(bbox)


def _colors_close(a: Tuple[int, int, int], b: Tuple[int, int, int], tol: int = 6) -> bool:
    return sum(abs(x - y) for x, y in zip(a, b)) <= tol


def _detect_border_color(img: Image.Image) -> Tuple[int, int, int] | None:
    if _has_alpha(img):
        return None
    rgb = img.convert("RGB")
    w, h = rgb.size
    samples = [
        rgb.getpixel((0, 0)),
        rgb.getpixel((w - 1, 0)),
        rgb.getpixel((0, h - 1)),
        rgb.getpixel((w - 1, h - 1)),
    ]
    # If all corners are reasonably close, treat as border color.
    base = samples[0]
    if all(_colors_close(base, s, tol=12) for s in samples[1:]):
        return base
    return None


def _trim_solid_border(img: Image.Image, tol: int = 8) -> Image.Image:
    # Trim borders that match a detected corner color.
    color = _detect_border_color(img)
    if color is None:
        return img
    rgb = img.convert("RGB")
    w, h = rgb.size

    def row_is_border(y: int) -> bool:
        for x in range(w):
            if not _colors_close(rgb.getpixel((x, y)), color, tol):
                return False
        return True

    def col_is_border(x: int) -> bool:
        for y in range(h):
            if not _colors_close(rgb.getpixel((x, y)), color, tol):
                return False
        return True

    top = 0
    while top < h and row_is_border(top):
        top += 1
    bottom = h - 1
    while bottom >= 0 and row_is_border(bottom):
        bottom -= 1
    left = 0
    while left < w and col_is_border(left):
        left += 1
    right = w - 1
    while right >= 0 and col_is_border(right):
        right -= 1

    if right < left or bottom < top:
        return img
    return img.crop((left, top, right + 1, bottom + 1))


def _enhance(img: Image.Image) -> Image.Image:
    # Work in RGBA if possible to preserve transparency.
    has_alpha = _has_alpha(img)
    if has_alpha and img.mode != "RGBA":
        img = img.convert("RGBA")
    if not has_alpha and img.mode != "RGB":
        img = img.convert("RGB")

    # Mild auto-contrast on color channels only
    if has_alpha:
        r, g, b, a = img.split()
        rgb = Image.merge("RGB", (r, g, b))
        rgb = ImageOps.autocontrast(rgb, cutoff=1)
        r2, g2, b2 = rgb.split()
        img = Image.merge("RGBA", (r2, g2, b2, a))
    else:
        img = ImageOps.autocontrast(img, cutoff=1)

    # Light denoise to reduce speckles (small median)
    img = img.filter(ImageFilter.MedianFilter(size=3))

    # Subtle unsharp mask to improve crispness
    img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=140, threshold=3))

    return img


def _pad(img: Image.Image, pad: int = 8) -> Image.Image:
    if pad <= 0:
        return img
    has_alpha = _has_alpha(img)
    mode = "RGBA" if has_alpha else "RGB"
    w, h = img.size
    bg = (0, 0, 0, 0) if has_alpha else (0, 0, 0)
    canvas = Image.new(mode, (w + pad * 2, h + pad * 2), bg)
    canvas.paste(img, (pad, pad), img if has_alpha else None)
    return canvas


def clean_image(path: Path, out_dir: Path, pad: int = 8) -> None:
    try:
        with Image.open(path) as im:
            im.load()
            # Trim transparency first; if no alpha, trim solid border.
            im2 = _trim_transparent(im)
            if im2 is im:
                im2 = _trim_solid_border(im2)
            im2 = _enhance(im2)
            im2 = _pad(im2, pad)

            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / path.name
            # Save with optimization; keep RGBA if present to preserve transparency.
            save_params = {"optimize": True}
            if _has_alpha(im2):
                im2.save(out_path, format="PNG", **save_params)
            else:
                # For non-alpha images, ensure PNG output unless the original was JPG.
                if path.suffix.lower() in {".jpg", ".jpeg"}:
                    im2.save(out_path.with_suffix(".jpg"), format="JPEG", quality=92, optimize=True)
                else:
                    im2.save(out_path, format="PNG", **save_params)
            print(f"✔ Cleaned: {path} -> {out_path}")
    except Exception as e:
        print(f"[WARN] Failed to clean {path}: {e}")


def main(argv: list[str]) -> int:
    # Defaults
    in_dir = Path("itemshop_section_images")
    out_dir = Path("itemshop_section_images_clean")
    pad = 8

    # Minimal CLI parsing (positional: [in_dir] [out_dir] [pad])
    if len(argv) >= 1 and argv[0]:
        in_dir = Path(argv[0])
    if len(argv) >= 2 and argv[1]:
        out_dir = Path(argv[1])
    if len(argv) >= 3 and argv[2]:
        try:
            pad = max(0, int(argv[2]))
        except ValueError:
            pass

    if not in_dir.exists():
        print(f"[ERROR] Input folder not found: {in_dir}")
        return 1

    exts = {".png", ".jpg", ".jpeg"}
    files = [p for p in in_dir.iterdir() if p.is_file() and p.suffix.lower() in exts]
    if not files:
        print(f"[INFO] No images found in: {in_dir}")
        return 0

    for p in files:
        clean_image(p, out_dir, pad=pad)
    print(f"Done. Output -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

