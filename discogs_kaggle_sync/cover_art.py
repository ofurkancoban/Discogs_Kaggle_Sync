"""Generates a month-branded cover image for the Kaggle dataset.

Matches the final, user-tuned drop-shadow text-on-image logic from DiscogsGUI's native
Swift Cover Art tool (CoverArtRenderer.swift) rather than the original Python main.py
values - line spacing and the year/month vertical nudges were adjusted repeatedly there
based on visual feedback, and the shadow was simplified (no spread/dilation, just
angle/distance/blur/opacity) after the spread-based mask approach produced a misplaced,
wrongly-colored shadow. This mirrors that final version so both projects render the same look.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS_DIR = Path(__file__).parent.parent / "assets"
DEFAULT_BASE_IMAGE = ASSETS_DIR / "cover_art.png"
DEFAULT_FONT = ASSETS_DIR / "dreamorphanagehv-regular.otf"

MONTH_NAMES = [
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
    "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
]

# Matches ShadowParams in CoverArtRenderer.swift.
SHADOW_ANGLE_DEGREES = 30
SHADOW_DISTANCE = 3
SHADOW_BLUR_RADIUS = 12
SHADOW_OPACITY = 0.85

# Unlike CoverArtRenderer.swift's plain vinyl base image, this project's base image
# (assets/cover_art.png) already has "Discogs" + "Dataset Project" baked in, occupying
# roughly y=60-475, with a designated empty band from y=480-810 for the year/month text
# before the social-icons row starts at y=815. These values center the year/month block
# in that empty band instead of reusing the Swift app's tuning, which doesn't apply here.
LINE_SPACING = 30
Y_OFFSET = 194
YEAR_VERTICAL_NUDGE = 0
MONTH_VERTICAL_NUDGE = 0


def _draw_text_with_drop_shadow(image: Image.Image, text: str, position: tuple[float, float], font) -> None:
    dx = int(round(SHADOW_DISTANCE * math.cos(math.radians(SHADOW_ANGLE_DEGREES))))
    dy = int(round(SHADOW_DISTANCE * math.sin(math.radians(SHADOW_ANGLE_DEGREES))))
    base_x, base_y = int(round(position[0])), int(round(position[1]))

    dummy_draw = ImageDraw.Draw(image)
    bbox = dummy_draw.textbbox((0, 0), text, font=font)
    text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]

    # PIL's draw.text((x, y), ...) positions the pen origin, not the tight ink bounding
    # box - bbox[0]/bbox[1] (left/top bearing) can be non-zero and differs per string
    # (e.g. "2026" vs "SEPTEMBER"), so drawing directly at (base_x, base_y) without this
    # correction silently misaligns strings that were centered using their tight bbox
    # widths (as generate_cover_image does). This is exactly what caused the year/month
    # to visibly not line up.
    pen_x = base_x - bbox[0]
    pen_y = base_y - bbox[1]

    # No spread/dilation step (dropped in the Swift version - it relied on Core Image's
    # CIMorphologyMaximum, which isn't needed here and complicated the mask for no visual
    # benefit at these text sizes). Just render the glyph shape and blur it.
    mask = Image.new("L", (text_width, text_height), 0)
    ImageDraw.Draw(mask).text((-bbox[0], -bbox[1]), text, font=font, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=SHADOW_BLUR_RADIUS))

    shadow = Image.new("RGBA", mask.size, (0, 0, 0, 255))
    shadow.putalpha(mask.point(lambda a: int(a * SHADOW_OPACITY)))
    # The mask's (0, 0) corresponds to the tight-bbox top-left, which is exactly
    # (base_x, base_y) by construction above, so the shadow offset doesn't need the
    # bbox correction - only the real glyph draw below (via draw.text's pen semantics) does.
    image.alpha_composite(shadow, dest=(base_x + dx, base_y + dy))

    ImageDraw.Draw(image).text((pen_x, pen_y), text, font=font, fill=(255, 255, 255, 255))


def generate_cover_image(
    month: str,  # "YYYY-MM"
    output_path: Path,
    base_image_path: Path = DEFAULT_BASE_IMAGE,
    font_path: Path = DEFAULT_FONT,
    font_size: int = 150,
) -> Path:
    year, month_num = month.split("-")
    month_text = MONTH_NAMES[int(month_num) - 1]

    with Image.open(base_image_path) as img:
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        try:
            font = ImageFont.truetype(str(font_path), size=font_size)
        except OSError:
            font = ImageFont.load_default()

        draw = ImageDraw.Draw(img)
        year_bbox = draw.textbbox((0, 0), year, font=font)
        year_w, year_h = year_bbox[2] - year_bbox[0], year_bbox[3] - year_bbox[1]
        month_bbox = draw.textbbox((0, 0), month_text, font=font)
        month_w, month_h = month_bbox[2] - month_bbox[0], month_bbox[3] - month_bbox[1]

        total_height = year_h + LINE_SPACING + month_h
        block_width = max(year_w, month_w)
        x_start = (img.width - block_width) / 2
        y_start = (img.height - total_height) / 2 + Y_OFFSET

        _draw_text_with_drop_shadow(
            img, year,
            (x_start + (block_width - year_w) / 2, y_start + YEAR_VERTICAL_NUDGE),
            font,
        )
        _draw_text_with_drop_shadow(
            img, month_text,
            (x_start + (block_width - month_w) / 2, y_start + year_h + LINE_SPACING + MONTH_VERTICAL_NUDGE),
            font,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        img.convert("RGB").save(output_path, "PNG")

    return output_path
