#!/usr/bin/env python3
"""
make_icons.py — generate the PC Caster brand icon (a cast glyph: a
screen with wifi waves) and export every size the app + Roku channel need.

Run:  python make_icons.py
"""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
ROKU_IMG = os.path.join(HERE, "roku_receiver", "images")
os.makedirs(ASSETS, exist_ok=True)
os.makedirs(ROKU_IMG, exist_ok=True)

ACCENT = (88, 166, 255, 255)   # #58a6ff
DARK   = (13, 17, 23, 255)     # #0d1117
CARD   = (22, 27, 34, 255)     # #161b22

SS = 4  # supersample factor for smooth edges


def _rounded(draw, box, radius, **kw):
    draw.rounded_rectangle(box, radius=radius, **kw)


def draw_glyph(size, bg="dark"):
    """Play triangle flanked by broadcast/sound arcs on both sides."""
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if bg == "dark":
        _rounded(d, [0, 0, S - 1, S - 1], radius=int(S * 0.18), fill=DARK)
        _rounded(d, [int(S*0.04), int(S*0.04), int(S*0.96), int(S*0.96)],
                 radius=int(S * 0.16), outline=CARD, width=max(1, int(S*0.012)))

    cx, cy = S / 2, S / 2
    stroke = max(2, int(S * 0.052))
    fg = ACCENT

    # ── Play triangle (rounded outline, pointing right) ──
    ht = S * 0.150          # half height
    left = cx - S * 0.088
    right = cx + S * 0.150
    pts = [(left, cy - ht), (left, cy + ht), (right, cy), (left, cy - ht)]
    d.line(pts, fill=fg, width=stroke, joint="curve")
    # round the tips with small dots so corners look smooth
    for px, py in [(left, cy - ht), (left, cy + ht), (right, cy)]:
        r = stroke // 2
        d.ellipse([px - r, py - r, px + r, py + r], fill=fg)

    # ── Broadcast arcs on each side (two per side) ──
    for r_frac, span in ((0.265, 52), (0.375, 60)):
        r = S * r_frac
        box = [cx - r, cy - r, cx + r, cy + r]
        # left side (around 180deg / west)
        d.arc(box, start=180 - span, end=180 + span, fill=fg, width=stroke)
        # right side (around 0deg / east)
        d.arc(box, start=-span, end=span, fill=fg, width=stroke)

    return img.resize((size, size), Image.LANCZOS)


def fit_into(canvas_w, canvas_h, bg="dark"):
    """Draw the glyph on a non-square canvas (for Roku icon ratios)."""
    side = min(canvas_w, canvas_h)
    glyph = draw_glyph(side, bg="transparent")
    out = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    if bg == "dark":
        d = ImageDraw.Draw(out)
        _rounded(d, [0, 0, canvas_w - 1, canvas_h - 1],
                 radius=int(side * 0.12), fill=DARK)
    out.alpha_composite(glyph, ((canvas_w - side) // 2, (canvas_h - side) // 2))
    return out


def main():
    # Master + general PNG
    master = draw_glyph(512, bg="dark")
    master.save(os.path.join(ASSETS, "app_icon.png"))

    # Transparent glyph (for places that want no tile)
    draw_glyph(512, bg="transparent").save(os.path.join(ASSETS, "app_glyph.png"))

    # Windows multi-size .ico for window + taskbar
    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                 (128, 128), (256, 256)]
    master.save(os.path.join(ASSETS, "app_icon.ico"), sizes=ico_sizes)

    # Roku channel images (focus icons + splash). Roku ratios:
    fit_into(290, 218, bg="dark").save(os.path.join(ROKU_IMG, "icon_focus_hd.png"))
    fit_into(248, 140, bg="dark").save(os.path.join(ROKU_IMG, "icon_focus_sd.png"))
    fit_into(1280, 720, bg="dark").save(os.path.join(ROKU_IMG, "splash_hd.png"))
    fit_into(720, 480, bg="dark").save(os.path.join(ROKU_IMG, "splash_sd.png"))

    print("Icons written to:")
    print(" ", ASSETS)
    print(" ", ROKU_IMG)


if __name__ == "__main__":
    main()
