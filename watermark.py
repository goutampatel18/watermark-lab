
#!/usr/bin/env python3
"""Add a transparent watermark without rasterizing PDF pages.

Original PDFs remain unchanged. Watermarked PDFs are written to output/.
Existing page text, vectors, and images are preserved where possible.
"""

import hashlib
import io
import os
import time
from pathlib import Path

import fitz
from PIL import Image

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "source"
OUTPUT = ROOT / "output"
MARK = ROOT / "assets" / "watermark.png"

MARK_WIDTH_RATIO = 0.42
MARK_OPACITY = 0.12
MAX_MARK_WIDTH_PX = 1200


def load_mark() -> bytes:
    """Prepare a transparent PNG used only for the watermark."""
    if not MARK.is_file():
        raise FileNotFoundError(f"Missing watermark image: {MARK}")

    with Image.open(MARK) as source:
        mark = source.convert("RGBA")

    # Downsample only the watermark artwork, never the document pages.
    if mark.width > MAX_MARK_WIDTH_PX:
        new_height = round(
            mark.height * MAX_MARK_WIDTH_PX / mark.width
        )
        mark = mark.resize(
            (MAX_MARK_WIDTH_PX, new_height),
            Image.Resampling.LANCZOS,
        )

    # Reduce existing alpha while preserving transparent areas.
    red, green, blue, alpha = mark.split()
    alpha = alpha.point(
        lambda value: round(value * MARK_OPACITY)
    )
    mark = Image.merge("RGBA", (red, green, blue, alpha))

    buffer = io.BytesIO()
    mark.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def watermark_rect(
    page: fitz.Page,
    mark_width_px: int,
    mark_height_px: int,
) -> fitz.Rect:
    """Calculate a centered watermark rectangle in PDF points."""
    page_rect = page.rect

    max_width = page_rect.width * MARK_WIDTH_RATIO
    max_height = page_rect.height * 0.80

    # Keep the image within the page, preserving its aspect ratio.
    scale = min(
        max_width / mark_width_px,
        max_height / mark_height_px,
    )

    width = mark_width_px * scale
    height = mark_height_px * scale

    x0 = page_rect.x0 + (page_rect.width - width) / 2
    y0 = page_rect.y0 + (page_rect.height - height) / 2

    return fitz.Rect(x0, y0, x0 + width, y0 + height)


def sha256_file(path: Path) -> str:
    """Calculate a file hash without loading the entire file into memory."""
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def watermark_one(
    src: Path,
    dest: Path,
    watermark_png: bytes,
    mark_size: tuple[int, int],
) -> None:
    started = time.perf_counter()

    if src.resolve() == dest.resolve():
        raise ValueError("Source and destination must be different files.")

    dest.parent.mkdir(parents=True, exist_ok=True)

    # Use a temporary PDF so an interrupted save does not leave
    # a partially written final output.
    temp_dest = dest.with_name(f".{dest.stem}.tmp.pdf")

    try:
        if temp_dest.exists():
            temp_dest.unlink()

        with fitz.open(src) as doc:
            page_count = doc.page_count

            # Insert the image once, then reuse its PDF image resource
            # on subsequent pages.
            watermark_xref = 0

            for page in doc:
                rect = watermark_rect(
                    page,
                    mark_size[0],
                    mark_size[1],
                )

                watermark_xref = page.insert_image(
                    rect,
                    stream=watermark_png,
                    xref=watermark_xref,
                    overlay=True,
                    keep_proportion=True,
                )

            # Compress eligible streams and remove unused objects.
            # This does not rasterize or deliberately recompress
            # the original page images.
            doc.save(
                temp_dest,
                garbage=4,
                deflate=True,
            )

        # Verify that the output can be reopened and has all pages.
        with fitz.open(temp_dest) as check:
            if check.page_count != page_count:
                raise RuntimeError(
                    f"Page-count mismatch: expected {page_count}, "
                    f"got {check.page_count}"
                )

        # Publish the completed output only after validation.
        os.replace(temp_dest, dest)

        source_size = src.stat().st_size
        output_size = dest.stat().st_size
        difference = output_size - source_size
        percentage = (
            difference / source_size * 100 if source_size else 0
        )
        elapsed = time.perf_counter() - started

        print(
            f"{src.name} -> {dest.name} | "
            f"pages={page_count} | "
            f"src_bytes={source_size} | "
            f"out_bytes={output_size} | "
            f"size_change={percentage:+.2f}% | "
            f"seconds={elapsed:.2f} | "
            f"sha256={sha256_file(dest)}"
        )

    finally:
        if temp_dest.exists():
            temp_dest.unlink()


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)

    sources = sorted(SOURCE.glob("*.pdf"))
    if not sources:
        raise SystemExit(f"No PDFs found in {SOURCE}")

    watermark_png = load_mark()

    # Read dimensions from the prepared watermark asset.
    with Image.open(io.BytesIO(watermark_png)) as mark:
        mark_size = mark.size

    failed = 0

    for src in sources:
        try:
            watermark_one(
                src,
                OUTPUT / src.name,
                watermark_png,
                mark_size,
            )
        except Exception as exc:
            failed += 1
            print(f"FAILED: {src.name}: {exc}")

    print(
        f"\nCompleted: {len(sources) - failed}/{len(sources)} | "
        f"Failed: {failed}"
    )


if __name__ == "__main__":
    main()
