from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


SIZE = 1024
ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
PNG_PATH = ASSETS / "pdf2md-icon.png"
ICO_PATH = ASSETS / "pdf2md-icon.ico"


def rounded_line(
    draw: ImageDraw.ImageDraw,
    points: tuple[tuple[int, int], tuple[int, int]],
    *,
    fill: tuple[int, int, int, int],
    width: int,
) -> None:
    draw.line(points, fill=fill, width=width)
    radius = width // 2
    for x, y in points:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)


def build_icon() -> Image.Image:
    """Build a monochrome document line icon on a transparent canvas."""
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    ink = (22, 22, 22, 255)
    paper = (255, 255, 255, 255)
    clear = 0

    # Build the document from nested filled shapes instead of joining thick
    # line segments. This gives the 45-degree fold two exact, square joins
    # without line caps protruding past the top or right edge.
    outer_mask = Image.new("L", (SIZE, SIZE), clear)
    outer = ImageDraw.Draw(outer_mask)
    outer.rounded_rectangle((162, 82, 862, 942), radius=96, fill=255)
    outer.polygon(((638, 82), (863, 82), (863, 307)), fill=clear)
    canvas.paste(ink, (0, 0, SIZE, SIZE), outer_mask)

    border = 44
    diagonal_inset = round(border * (2**0.5))
    inner_mask = Image.new("L", (SIZE, SIZE), clear)
    inner = ImageDraw.Draw(inner_mask)
    inner.rounded_rectangle((206, 126, 818, 898), radius=56, fill=255)
    inner_fold_top = 126 + 556 - diagonal_inset
    inner_fold_right = 818 - 556 + diagonal_inset
    inner.polygon(
        ((inner_fold_top, 126), (819, 126), (819, inner_fold_right)),
        fill=clear,
    )
    canvas.paste(paper, (0, 0, SIZE, SIZE), inner_mask)

    draw = ImageDraw.Draw(canvas)
    fold_top = (638, 82)
    fold_corner = (638, 306)
    fold_right = (862, 306)
    draw.polygon((fold_top, fold_corner, fold_right), fill=ink)

    face_left = fold_corner[0] + border
    face_bottom = fold_corner[1] - border
    face_top = (face_left, face_left - 556 + diagonal_inset)
    face_right = (face_bottom + 556 - diagonal_inset, face_bottom)
    draw.polygon((face_top, (face_left, face_bottom), face_right), fill=paper)

    # The hash is deliberately heavy enough to stay legible at 16–24 px.
    rounded_line(draw, ((424, 395), (386, 718)), fill=ink, width=62)
    rounded_line(draw, ((609, 395), (571, 718)), fill=ink, width=62)
    rounded_line(draw, ((344, 495), (662, 495)), fill=ink, width=60)
    rounded_line(draw, ((329, 620), (647, 620)), fill=ink, width=60)
    return canvas


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    image = build_icon()
    preview = image.resize((256, 256), Image.Resampling.LANCZOS)
    preview.save(PNG_PATH, optimize=True)
    image.save(
        ICO_PATH,
        format="ICO",
        sizes=((16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)),
    )
    print(f"Built: {PNG_PATH}")
    print(f"Built: {ICO_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
