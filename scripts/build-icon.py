from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


SIZE = 1024
ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
PNG_PATH = ASSETS / "pdf2md-icon.png"
ICO_PATH = ASSETS / "pdf2md-icon.ico"


def blend(start: tuple[int, int, int], end: tuple[int, int, int], amount: float) -> tuple[int, int, int, int]:
    return tuple(round(a + (b - a) * amount) for a, b in zip(start, end)) + (255,)


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
    gradient = Image.new("RGBA", (SIZE, SIZE))
    gradient_draw = ImageDraw.Draw(gradient)
    top = (76, 84, 232)
    bottom = (24, 187, 220)
    for y in range(SIZE):
        gradient_draw.line((0, y, SIZE, y), fill=blend(top, bottom, y / (SIZE - 1)))

    mask = Image.new("L", (SIZE, SIZE))
    ImageDraw.Draw(mask).rounded_rectangle((48, 48, 976, 976), radius=218, fill=255)
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(gradient, mask=mask)

    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((-120, -150, 650, 620), fill=(183, 171, 255, 115))
    glow_draw.ellipse((500, 430, 1160, 1120), fill=(52, 232, 243, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(115))
    canvas.alpha_composite(glow)

    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((161, 170, 525, 826), radius=70, fill=(7, 15, 41, 135))
    shadow = shadow.filter(ImageFilter.GaussianBlur(34))
    canvas.alpha_composite(shadow)

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((138, 135, 510, 798), radius=64, fill=(248, 250, 255, 255))
    draw.polygon(((382, 135), (510, 263), (382, 263)), fill=(202, 213, 255, 255))
    draw.polygon(((382, 135), (510, 263), (510, 135)), fill=(94, 106, 235, 210))

    coral = (255, 96, 125, 255)
    rounded_line(draw, ((220, 395), (422, 395)), fill=coral, width=34)
    rounded_line(draw, ((220, 493), (391, 493)), fill=(112, 125, 160, 255), width=27)
    rounded_line(draw, ((220, 580), (418, 580)), fill=(112, 125, 160, 255), width=27)

    arrow = (224, 247, 255, 255)
    rounded_line(draw, ((500, 476), (655, 476)), fill=arrow, width=38)
    draw.polygon(((624, 407), (716, 476), (624, 545)), fill=arrow)

    hash_color = (255, 255, 255, 255)
    rounded_line(draw, ((742, 347), (692, 681)), fill=hash_color, width=42)
    rounded_line(draw, ((866, 347), (816, 681)), fill=hash_color, width=42)
    rounded_line(draw, ((680, 454), (885, 454)), fill=hash_color, width=42)
    rounded_line(draw, ((662, 580), (867, 580)), fill=hash_color, width=42)

    shine = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    shine_draw = ImageDraw.Draw(shine)
    shine_draw.arc((70, 70, 954, 954), start=198, end=305, fill=(255, 255, 255, 82), width=18)
    canvas.alpha_composite(shine)
    return canvas


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    image = build_icon()
    preview = image.resize((256, 256), Image.Resampling.LANCZOS)
    preview.save(PNG_PATH, optimize=True)
    image.save(
        ICO_PATH,
        format="ICO",
        sizes=((16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)),
    )
    print(f"Built: {PNG_PATH}")
    print(f"Built: {ICO_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

