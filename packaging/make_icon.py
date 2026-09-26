"""Draws mcsm's icon: blocky stone "MC / SM" letters, in the style of the Minecraft logo,
smashing into a brick wall. Needs Pillow (a development tool only; mcsm itself doesn't).

    pip install pillow && python packaging/make_icon.py

Writes packaging/mcsm.ico (the Windows executable), src/mcsm/webui/icon.png (the web
UI and the friend page) and docs/icon.png (the README). The drawing is seeded, so the
same code always makes the same icon.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
S = 1024  # drawing size; everything else is scaled down from it

GLYPHS = {
    "M": ["X...X", "XX.XX", "X.X.X", "X...X", "X...X", "X...X", "X...X"],
    "C": [".XXXX", "X....", "X....", "X....", "X....", "X....", ".XXXX"],
    "S": [".XXXX", "X....", "X....", ".XXX.", "....X", "....X", "XXXX."],
}


def noise(size, rng, lo=0, hi=255, blur=0.0):
    img = Image.effect_noise(size, 64).point(lambda v: max(lo, min(hi, v)))
    return img.filter(ImageFilter.GaussianBlur(blur)) if blur else img


# ------------------------------------------------------------------- bricks
def brick_wall(rng: random.Random) -> Image.Image:
    wall = Image.new("RGB", (S, S), (176, 168, 156))  # mortar
    mortar_tex = noise((S, S), rng, 170, 255, 0.6).convert("RGB")
    wall = ImageChops.multiply(wall, mortar_tex)
    draw = ImageDraw.Draw(wall)
    bw, bh, gap = 232, 96, 16
    palette = [(178, 72, 48), (192, 86, 56), (160, 62, 42), (204, 102, 68), (150, 58, 40), (186, 94, 62)]
    shade = Image.new("L", (S, S), 0)
    sd = ImageDraw.Draw(shade)
    for row in range(-1, S // (bh + gap) + 2):
        y = row * (bh + gap) + 6
        offset = -(bw // 2) if row % 2 else 0
        for col in range(-1, S // (bw + gap) + 2):
            x = col * (bw + gap) + offset
            base = rng.choice(palette)
            jitter = rng.randint(-10, 10)
            color = tuple(max(0, min(255, c + jitter)) for c in base)
            box = [x + rng.randint(0, 3), y + rng.randint(0, 3), x + bw - rng.randint(0, 3), y + bh - rng.randint(0, 3)]
            draw.rounded_rectangle(box, radius=10, fill=color)
            # light from the top left: bright top edge, dark bottom edge
            sd.rounded_rectangle([box[0], box[1], box[2], box[1] + 10], radius=6, fill=70)
            sd.rounded_rectangle([box[0], box[3] - 12, box[2], box[3]], radius=6, fill=200)
    grit = noise((S, S), rng, 185, 255, 0.8).convert("RGB")
    wall = ImageChops.multiply(wall, grit)
    spots = noise((S // 8, S // 8), rng, 0, 255).resize((S, S), Image.BICUBIC).filter(ImageFilter.GaussianBlur(14))
    spots = spots.point(lambda v: 150 + v * 105 // 255)
    wall = Image.blend(wall, ImageChops.multiply(wall, Image.merge("RGB", [spots] * 3)), 0.5)
    light = Image.new("RGB", (S, S), (255, 240, 220))
    dark = Image.new("RGB", (S, S), (20, 12, 10))
    top = shade.point(lambda v: 120 if v == 70 else 0).filter(ImageFilter.GaussianBlur(3))
    bottom = shade.point(lambda v: 150 if v == 200 else 0).filter(ImageFilter.GaussianBlur(4))
    wall = Image.composite(light, wall, top.point(lambda v: v // 3))
    wall = Image.composite(dark, wall, bottom.point(lambda v: v // 2))
    # vignette
    vig = Image.radial_gradient("L").resize((S, S)).point(lambda v: int(max(0, v - 90) * 0.9))
    return Image.composite(Image.new("RGB", (S, S), (10, 6, 5)), wall, vig)


# ------------------------------------------------------------------ impact
def crater(wall: Image.Image, center, rng: random.Random) -> Image.Image:
    """Cracks, knocked-out chunks and a dark hollow where the letters hit."""
    cx, cy = center
    hollow = Image.new("L", (S, S), 0)
    hd = ImageDraw.Draw(hollow)
    pts = []
    for i in range(28):
        a = i / 28 * 2 * math.pi
        r = rng.uniform(330, 430)
        pts.append((cx + math.cos(a) * r * 1.05, cy + math.sin(a) * r * 0.95))
    hd.polygon(pts, fill=95)
    hollow = hollow.filter(ImageFilter.GaussianBlur(40))
    wall = Image.composite(Image.new("RGB", (S, S), (25, 14, 10)), wall, hollow)

    draw = ImageDraw.Draw(wall)
    # chunks knocked out around the edge of the impact
    for _ in range(26):
        a = rng.uniform(0, 2 * math.pi)
        r = rng.uniform(300, 440)
        x, y = cx + math.cos(a) * r, cy + math.sin(a) * r
        size = rng.uniform(18, 48)
        poly = [(x + math.cos(t) * size * rng.uniform(0.5, 1.2), y + math.sin(t) * size * rng.uniform(0.5, 1.2))
                for t in [k / 7 * 2 * math.pi for k in range(7)]]
        draw.polygon(poly, fill=(38, 22, 16))
        draw.line(poly[:3], fill=(190, 110, 80), width=3)  # lit broken edge
    # cracks
    for _ in range(18):
        a = rng.uniform(0, 2 * math.pi)
        x, y = cx + math.cos(a) * 280, cy + math.sin(a) * 260
        length = rng.uniform(160, 420)
        width = 9
        while length > 0 and width > 1:
            step = rng.uniform(22, 48)
            a += rng.uniform(-0.45, 0.45)
            nx, ny = x + math.cos(a) * step, y + math.sin(a) * step
            draw.line([(x, y), (nx, ny)], fill=(18, 10, 8), width=int(width))
            draw.line([(x + 2, y + 2), (nx + 2, ny + 2)], fill=(200, 130, 100), width=max(1, int(width / 3)))
            if rng.random() < 0.18:  # branch
                b = a + rng.choice([-1, 1]) * rng.uniform(0.5, 1.0)
                draw.line([(nx, ny), (nx + math.cos(b) * step * 1.4, ny + math.sin(b) * step * 1.4)],
                          fill=(18, 10, 8), width=max(1, int(width / 2)))
            x, y, length, width = nx, ny, length - step, width * 0.9
    return wall


def debris(img: Image.Image, center, rng: random.Random) -> Image.Image:
    cx, cy = center
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(90):  # dust
        a = rng.uniform(0, 2 * math.pi)
        r = rng.uniform(250, 520)
        x, y = cx + math.cos(a) * r, cy + math.sin(a) * r
        s = rng.uniform(20, 70)
        d.ellipse([x - s, y - s, x + s, y + s], fill=(200, 180, 160, rng.randint(20, 50)))
    layer = layer.filter(ImageFilter.GaussianBlur(16))
    d = ImageDraw.Draw(layer)
    for _ in range(30):  # flying brick chips, bigger the further out
        a = rng.uniform(0, 2 * math.pi)
        r = rng.uniform(360, 560)
        x, y = cx + math.cos(a) * r, cy + math.sin(a) * r
        s = rng.uniform(6, 22) * (r / 420)
        rot = rng.uniform(0, math.pi)
        poly = [(x + math.cos(rot + k * 2.1) * s * rng.uniform(0.6, 1.3), y + math.sin(rot + k * 2.1) * s * rng.uniform(0.6, 1.3))
                for k in range(3 + rng.randint(0, 2))]
        c = rng.choice([(160, 70, 48), (120, 48, 34), (180, 96, 66), (150, 140, 128)])
        d.polygon(poly, fill=c + (255,))
        d.line(poly[:2], fill=(230, 170, 130, 255), width=2)
    return Image.alpha_composite(img.convert("RGBA"), layer)


# ------------------------------------------------------------------ letters
def stone_block(size: int, rng: random.Random) -> Image.Image:
    """One cobbled stone block, like the Minecraft logo's letters."""
    base = rng.randint(128, 150)
    blk = Image.new("RGB", (size, size), (base, base, base + 4))
    d = ImageDraw.Draw(blk)
    px = max(2, size // 8)
    for yy in range(0, size, px):
        for xx in range(0, size, px):
            v = base + rng.choice([-34, -20, -10, 0, 0, 8, 16, 26])
            d.rectangle([xx, yy, xx + px - 1, yy + px - 1], fill=(v, v, v + 4))
    b = max(2, size // 10)
    d.rectangle([0, 0, size - 1, b - 1], fill=(206, 206, 210))           # lit top
    d.rectangle([0, 0, b - 1, size - 1], fill=(188, 188, 192))           # lit left
    d.rectangle([0, size - b, size - 1, size - 1], fill=(72, 72, 76))    # shaded bottom
    d.rectangle([size - b, 0, size - 1, size - 1], fill=(92, 92, 96))    # shaded right
    return blk


def letters(rng: random.Random) -> tuple[Image.Image, tuple[int, int]]:
    rows = ["MC", "SM"]
    cell = 56
    cols = 11
    lines = 15
    w, h = cols * cell, lines * cell
    depth = int(cell * 0.62)
    img = Image.new("RGBA", (w + depth + 40, h + depth + 40), (0, 0, 0, 0))
    mask = Image.new("L", img.size, 0)
    cells = []
    for r, word in enumerate(rows):
        for i, ch in enumerate(word):
            for gy, line in enumerate(GLYPHS[ch]):
                for gx, on in enumerate(line):
                    if on == "X":
                        cells.append((20 + (i * 6 + gx) * cell, 20 + (r * 8 + gy) * cell))
    # the extruded sides (down and to the right), darkest at the back
    for k in range(depth, 0, -2):
        shade = int(28 + 50 * (1 - k / depth))
        side = Image.new("RGBA", (cell, cell), (shade, shade, shade + 3, 255))
        for x, y in cells:
            img.paste(side, (x + k, y + k))
            mask.paste(255, (x + k, y + k, x + k + cell, y + k + cell))
    for x, y in cells:
        img.paste(stone_block(cell, rng), (x, y))
        mask.paste(255, (x, y, x + cell, y + cell))
    # a thick black outline around the whole shape, as on the logo
    outline = mask.filter(ImageFilter.MaxFilter(13))
    back = Image.new("RGBA", img.size, (12, 12, 14, 255))
    back.putalpha(outline)
    img = Image.alpha_composite(back, img)
    return img, (w, h)


def make() -> Image.Image:
    rng = random.Random(20260926)
    center = (S // 2, S // 2 + 10)
    wall = crater(brick_wall(rng), center, rng)
    art = debris(wall, center, rng)
    text, _ = letters(rng)
    # Hitting at an angle: tilted, a little bigger than the hole.
    text = text.rotate(-7, resample=Image.BICUBIC, expand=True)
    scale = 0.9 * S / max(text.size)
    text = text.resize((int(text.width * scale), int(text.height * scale)), Image.LANCZOS)
    x, y = center[0] - text.width // 2, center[1] - text.height // 2
    shadow = Image.new("RGBA", art.size, (0, 0, 0, 0))
    alpha = text.getchannel("A").point(lambda v: int(v * 0.8))
    shadow.paste(Image.new("RGBA", text.size, (0, 0, 0, 255)), (x + 30, y + 38), alpha)
    art = Image.alpha_composite(art, shadow.filter(ImageFilter.GaussianBlur(22)))
    art.alpha_composite(text, (x, y))
    # rounded corners
    corner = Image.new("L", (S, S), 0)
    ImageDraw.Draw(corner).rounded_rectangle([0, 0, S - 1, S - 1], radius=150, fill=255)
    art.putalpha(ImageChops.multiply(art.getchannel("A"), corner))
    return art


def main() -> None:
    art = make()
    sizes = [16, 24, 32, 48, 64, 128, 256]
    big = art.resize((256, 256), Image.LANCZOS)
    big.save(ROOT / "packaging" / "mcsm.ico", sizes=[(s, s) for s in sizes])
    big.save(ROOT / "src" / "mcsm" / "webui" / "icon.png", optimize=True)
    art.resize((512, 512), Image.LANCZOS).save(ROOT / "docs" / "icon.png", optimize=True)
    print("wrote packaging/mcsm.ico, src/mcsm/webui/icon.png, docs/icon.png")


if __name__ == "__main__":
    main()
