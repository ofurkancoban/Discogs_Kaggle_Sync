"""Generates a month-branded cover image for the Kaggle dataset.

Matches the final, user-tuned drop-shadow text-on-image logic from DiscogsGUI's native
Swift Cover Art tool (CoverArtRenderer.swift) rather than the original Python main.py
values - line spacing and the year/month vertical nudges were adjusted repeatedly there
based on visual feedback, and the shadow was simplified (no spread/dilation, just
angle/distance/blur/opacity) after the spread-based mask approach produced a misplaced,
wrongly-colored shadow. This mirrors that final version so both projects render the same look.
"""
from __future__ import annotations

import base64
import logging
import math
import os
import random
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).parent.parent / "assets"
DEFAULT_BASE_IMAGE = ASSETS_DIR / "cover_art.png"
DEFAULT_LOGO = ASSETS_DIR / "logo.png"
DEFAULT_FONT = ASSETS_DIR / "dreamorphanagehv-regular.otf"
ENV_FILE = ASSETS_DIR.parent / ".env"

IMAGE_WIDTH = 1344
IMAGE_HEIGHT = 672

# Tuned through visual A/B testing (see conversation history, not committed): wide-angle +
# "record shop" framing reads as a real shop instead of a warehouse, candid/asymmetric
# framing avoids the stiffness of a dead-center composition, and "plain dim wall space in
# upper area" keeps that area clear enough for the logo + year/month text to stay legible.
BACKGROUND_PROMPT = (
    "vintage analog film photograph, muted natural colors with warm undertones, moderately "
    "dark moody lighting but details still visible, subtle film grain, 1970s record shop "
    "aesthetic, ultra wide angle lens, candid off-center angle, natural asymmetric "
    "composition, highly detailed, wide angle photo of a lived-in record shop caught "
    "candidly, stacks and crates of vinyl records slightly askew, posters and memorabilia "
    "on the walls, warm lamp lighting, plain dim wall space in upper area, sharp focus"
)
BACKGROUND_NEGATIVE_PROMPT = (
    "text, letters, words, signage, neon sign, writing, typography, watermark, logo, "
    "people, faces, blurry, low quality, distorted, cropped, oversaturated, vibrant "
    "colors, neon colors, bright white light, overexposed, washed out, monochrome, "
    "single color tint, warehouse, storage room, archive, floor to ceiling shelving, "
    "industrial, symmetric, centered composition"
)

# Cloudflare Workers AI (free tier: 10,000 neurons/day, resets daily - well suited to a
# once-a-month cover generation). Tried first since lucid-origin's output quality was
# consistently better than Hugging Face's in side-by-side testing.
CF_MODEL = "@cf/leonardo/lucid-origin"

# Hugging Face's router-based Inference Providers API (free tier: a small monthly credit
# allowance that a burst of use - e.g. prompt-testing, or several backfilled months in a
# row - can exhaust for the rest of the month). Kept as a secondary fallback since it's a
# different quota pool than Cloudflare's, not because it's the preferred provider.
# stable-diffusion-3-medium-diffusers is the only text-to-image model currently live on
# the free "hf-inference" provider (most popular ones, e.g. FLUX/SDXL, have been
# deprecated there in favor of paid providers).
HF_API_URL = "https://router.huggingface.co/hf-inference/models/stabilityai/stable-diffusion-3-medium-diffusers"

# Every pixel constant below (Y_OFFSET, LINE_SPACING, font_size) was tuned against a
# background image this tall - a differently-sized base image (e.g. an AI-generated one)
# gets these scaled by its own height / this reference, so the text block lands in
# roughly the same relative spot instead of drifting or getting clipped off the bottom.
REFERENCE_HEIGHT = 902

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
    #
    # The mask canvas needs padding on every side, or the Gaussian blur has nowhere to
    # fade into and gets hard-clipped at the canvas edge instead of softening naturally -
    # visible as the shadow looking "cut off" right at the glyph's bounding box.
    pad = SHADOW_BLUR_RADIUS * 3
    mask = Image.new("L", (text_width + 2 * pad, text_height + 2 * pad), 0)
    ImageDraw.Draw(mask).text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=SHADOW_BLUR_RADIUS))

    shadow = Image.new("RGBA", mask.size, (0, 0, 0, 255))
    shadow.putalpha(mask.point(lambda a: int(a * SHADOW_OPACITY)))
    # The mask's (pad, pad) corresponds to the tight-bbox top-left, which is exactly
    # (base_x, base_y) by construction above, so the shadow offset (after subtracting the
    # padding back out) doesn't need the bbox correction - only the real glyph draw below
    # (via draw.text's pen semantics) does.
    image.alpha_composite(shadow, dest=(base_x - pad + dx, base_y - pad + dy))

    ImageDraw.Draw(image).text((pen_x, pen_y), text, font=font, fill=(255, 255, 255, 255))


def generate_cover_image(
    month: str,  # "YYYY-MM"
    output_path: Path,
    base_image_path: Path = DEFAULT_BASE_IMAGE,
    logo_path: Path | None = DEFAULT_LOGO,
    font_path: Path = DEFAULT_FONT,
    font_size: int = 150,
) -> Path:
    """`base_image_path` is the plain background photo; `logo_path` (if given and it
    exists) is a transparent "Discogs" + "Dataset Project" overlay, composited on top
    before the year/month text - decoupling the branding from the background so a fresh
    AI-generated photo can be dropped in each month without also having to bake the logo
    into it. Pass logo_path=None to skip it (e.g. for a background that already has its
    own baked-in branding, like the original assets/cover_art.png)."""
    year, month_num = month.split("-")
    month_text = MONTH_NAMES[int(month_num) - 1]

    with Image.open(base_image_path) as img:
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        scale = img.height / REFERENCE_HEIGHT

        if logo_path is not None and logo_path.exists():
            with Image.open(logo_path) as logo:
                if logo.mode != "RGBA":
                    logo = logo.convert("RGBA")
                if logo.size != img.size:
                    logo = logo.resize(img.size, Image.LANCZOS)
                img.alpha_composite(logo)

        try:
            font = ImageFont.truetype(str(font_path), size=round(font_size * scale))
        except OSError:
            font = ImageFont.load_default()

        draw = ImageDraw.Draw(img)
        year_bbox = draw.textbbox((0, 0), year, font=font)
        year_w, year_h = year_bbox[2] - year_bbox[0], year_bbox[3] - year_bbox[1]
        month_bbox = draw.textbbox((0, 0), month_text, font=font)
        month_w, month_h = month_bbox[2] - month_bbox[0], month_bbox[3] - month_bbox[1]

        line_spacing = LINE_SPACING * scale
        total_height = year_h + line_spacing + month_h
        block_width = max(year_w, month_w)
        x_start = (img.width - block_width) / 2
        y_start = (img.height - total_height) / 2 + Y_OFFSET * scale

        _draw_text_with_drop_shadow(
            img, year,
            (x_start + (block_width - year_w) / 2, y_start + YEAR_VERTICAL_NUDGE * scale),
            font,
        )
        _draw_text_with_drop_shadow(
            img, month_text,
            (x_start + (block_width - month_w) / 2, y_start + year_h + line_spacing + MONTH_VERTICAL_NUDGE * scale),
            font,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        img.convert("RGB").save(output_path, "PNG")

    return output_path


def _env_var(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return None


def generate_cf_background(output_path: Path, seed: int | None = None) -> Path:
    """Generates a fresh record-shop background photo via Cloudflare Workers AI's free
    lucid-origin model. A random seed each call is the point - every month gets a
    visually distinct photo instead of reusing one background forever. Raises on missing
    credentials or a failed/slow request; generate_monthly_cover catches that and tries
    the next provider, so a sync run is never blocked on this."""
    account_id = _env_var("CF_ACCOUNT_ID")
    token = _env_var("CF_API_TOKEN")
    if not account_id or not token:
        raise RuntimeError("No CF_ACCOUNT_ID/CF_API_TOKEN found in .env for AI cover art generation.")
    if seed is None:
        seed = random.randint(0, 2**31 - 1)

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{CF_MODEL}"
    payload = {
        "prompt": BACKGROUND_PROMPT,
        "negative_prompt": BACKGROUND_NEGATIVE_PROMPT,
        "width": IMAGE_WIDTH,
        "height": IMAGE_HEIGHT,
        "seed": seed,
    }
    response = requests.post(
        url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"Cloudflare Workers AI request failed: {data.get('errors')}")

    image_bytes = base64.b64decode(data["result"]["image"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(image_bytes)
    logger.info("Generated AI cover background via Cloudflare (seed=%d) -> %s", seed, output_path)
    return output_path


def generate_hf_background(output_path: Path, seed: int | None = None) -> Path:
    """Generates a fresh record-shop background photo via Hugging Face's free
    stable-diffusion-3-medium inference endpoint. Secondary fallback behind Cloudflare -
    see the HF_API_URL comment for why. Raises on a missing token or a failed/slow
    request; generate_monthly_cover is what catches that and falls back further."""
    token = _env_var("HF")
    if not token:
        raise RuntimeError("No HF token found (set HF=<token> in .env) for AI cover art generation.")
    if seed is None:
        seed = random.randint(0, 2**31 - 1)

    payload = {
        "inputs": BACKGROUND_PROMPT,
        "parameters": {
            "width": IMAGE_WIDTH,
            "height": IMAGE_HEIGHT,
            "negative_prompt": BACKGROUND_NEGATIVE_PROMPT,
            "seed": seed,
        },
    }
    response = requests.post(
        HF_API_URL, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=120,
    )
    response.raise_for_status()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(response.content)
    logger.info("Generated AI cover background via Hugging Face (seed=%d) -> %s", seed, output_path)
    return output_path


def generate_monthly_cover(month: str, output_path: Path, seed: int | None = None) -> Path:
    """Entry point sync callers should use: a fresh AI-generated background each month -
    Cloudflare first, Hugging Face second (a separate quota pool, in case Cloudflare's
    daily allowance or Hugging Face's monthly one is exhausted) - falling back to the
    static base image (with its own baked-in logo) if neither AI provider is available."""
    ai_background_path = output_path.parent / f".ai_background_{month}.jpg"
    for name, generate in (("Cloudflare", generate_cf_background), ("Hugging Face", generate_hf_background)):
        try:
            generate(ai_background_path, seed=seed)
            return generate_cover_image(month, output_path, base_image_path=ai_background_path, logo_path=DEFAULT_LOGO)
        except Exception as e:
            logger.warning("%s cover background generation failed: %s", name, e)
        finally:
            ai_background_path.unlink(missing_ok=True)

    logger.warning("All AI cover providers failed; using the static base image instead.")
    return generate_cover_image(month, output_path, base_image_path=DEFAULT_BASE_IMAGE, logo_path=None)
