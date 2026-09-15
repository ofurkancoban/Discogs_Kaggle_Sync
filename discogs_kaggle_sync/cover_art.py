"""Generates a month-branded cover image for the Kaggle dataset, reusing the same
drop-shadow text-on-image technique as DiscogsGUI's Cover Art tool (main.py).
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSETS_DIR = Path(__file__).parent.parent / "assets"
DEFAULT_BASE_IMAGE = ASSETS_DIR / "cover_art.png"
DEFAULT_FONT = ASSETS_DIR / "dreamorphanagehv-regular.otf"

MONTH_NAMES = [
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
    "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
]


def _draw_text_with_drop_shadow(image, text, position, font, text_color, shadow_params):
    angle = shadow_params.get("angle", 30)
    distance = shadow_params.get("distance", 1)
    spread = shadow_params.get("spread", 0.002)
    blur_radius = shadow_params.get("blur_radius", 10)

    from PIL import ImageFilter

    dx = int(round(distance * math.cos(math.radians(angle))))
    dy = int(round(distance * math.sin(math.radians(angle))))
    base_x, base_y = int(round(position[0])), int(round(position[1]))

    dummy_draw = ImageDraw.Draw(image)
    bbox = dummy_draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    spread_pixels = int(max(text_width, text_height) * spread)

    mask_size = (text_width + 2 * spread_pixels, text_height + 2 * spread_pixels)
    mask = Image.new("L", mask_size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.text((spread_pixels, spread_pixels), text, font=font, fill=255)
    if spread_pixels > 0:
        mask = mask.filter(ImageFilter.MaxFilter(spread_pixels * 2 + 1))
    mask = mask.filter(ImageFilter.GaussianBlur(radius=blur_radius))

    shadow = Image.new("RGBA", mask_size, (0, 0, 0, 255))
    shadow.putalpha(mask)
    shadow_position = (base_x - spread_pixels + dx, base_y - spread_pixels + dy)
    image.alpha_composite(shadow, dest=shadow_position)

    draw = ImageDraw.Draw(image)
    draw.text((base_x, base_y), text, font=font, fill=text_color)


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
            year_font = ImageFont.truetype(str(font_path), size=font_size)
            month_font = year_font
        except OSError:
            year_font = month_font = ImageFont.load_default()

        shadow_params = {"angle": 30, "distance": 1, "spread": 0.002, "blur_radius": 10}
        text_color = (255, 255, 255, 255)

        draw = ImageDraw.Draw(img)
        year_bbox = draw.textbbox((0, 0), year, font=year_font)
        year_w, year_h = year_bbox[2] - year_bbox[0], year_bbox[3] - year_bbox[1]
        month_bbox = draw.textbbox((0, 0), month_text, font=month_font)
        month_w, month_h = month_bbox[2] - month_bbox[0], month_bbox[3] - month_bbox[1]

        line_spacing = 50
        total_height = year_h + line_spacing + month_h
        block_width = max(year_w, month_w)
        y_offset = 150
        x_start = (img.width - block_width) / 2
        y_start = (img.height - total_height) / 2 + y_offset

        _draw_text_with_drop_shadow(
            img, year, (x_start + (block_width - year_w) / 2, y_start),
            year_font, text_color, shadow_params,
        )
        _draw_text_with_drop_shadow(
            img, month_text, (x_start + (block_width - month_w) / 2, y_start + year_h + line_spacing),
            month_font, text_color, shadow_params,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        img.convert("RGB").save(output_path, "PNG")

    return output_path
