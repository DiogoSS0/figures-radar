"""Validated retailer image download and deterministic local figure cards."""
from __future__ import annotations

import hashlib
import io
import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MIN_DIMENSION = 200
MAX_DIMENSION = 6000
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def download_product_image(url: str, assets: Path, *, session: requests.Session | None = None) -> Path:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Image must use an HTTPS URL without credentials")
    if parsed.hostname not in {"media1.nin-nin-game.com", "media2.nin-nin-game.com", "www.hlj.com"}:
        raise ValueError("Image host is not an approved retailer host")
    response = (session or requests.Session()).get(url, stream=True, timeout=(8, 20), allow_redirects=False,
                                                  headers={"User-Agent": "FiguresRadar/0.1 (+https://github.com/DiogoSS0/figures-radar)"})
    try:
        if response.status_code != 200:
            raise ValueError(f"Image HTTP {response.status_code}")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
        if content_type not in ALLOWED_TYPES:
            raise ValueError("Invalid image Content-Type")
        declared = response.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
            raise ValueError("Image too large")
        data = bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("Image too large")
    finally:
        response.close()
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("Unsupported image format")
            width, height = probe.size
            if min(width, height) < MIN_DIMENSION or max(width, height) > MAX_DIMENSION or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("Invalid image dimensions")
            probe.verify()
        with Image.open(io.BytesIO(data)) as decoded:
            image = ImageOps.exif_transpose(decoded).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid image payload") from exc
    assets.mkdir(parents=True, exist_ok=True)
    target = assets / (hashlib.sha256(data).hexdigest() + ".jpg")
    if not target.exists():
        image.save(target, format="JPEG", quality=92, optimize=True)
    return target


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


def _fit_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont,
               width: int, max_lines: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    while words and len(lines) < max_lines:
        line = words.pop(0)
        while words and draw.textbbox((0, 0), line + " " + words[0], font=font)[2] <= width:
            line += " " + words.pop(0)
        if words and len(lines) == max_lines - 1:
            while line and draw.textbbox((0, 0), line + "…", font=font)[2] > width:
                line = line.rsplit(" ", 1)[0] if " " in line else line[:-1]
            line += "…"
        lines.append(line)
    return lines


def _display_title(title: str) -> str:
    title = re.sub(r"\s*\[[^]]+\]\s*$", "", title).strip()
    if title.casefold().startswith("nendoroid ") and " - " in title:
        trailing = title.rsplit(" - ", 1)[-1]
        if len(trailing) >= 12:
            return trailing
    return title


def _crop_black_letterbox(image: Image.Image) -> Image.Image:
    width, height = image.size
    limit = height // 4
    top = 0
    while top < limit and max(channel[1] for channel in image.crop((0, top, width, top + 1)).getextrema()) < 25:
        top += 1
    bottom = height
    while height - bottom < limit and max(channel[1] for channel in image.crop((0, bottom - 1, width, bottom)).getextrema()) < 25:
        bottom -= 1
    return image.crop((0, top, width, bottom)) if top or bottom < height else image


def _money(value: Decimal, currency: str) -> str:
    if currency == "EUR":
        return f"€{value:,.2f}"
    if currency == "USD":
        return f"${value:,.2f}"
    if currency == "JPY":
        return f"¥{value:,.0f}"
    return f"{value:,.2f} {currency}"


def render_card(deal: dict, product_image: Path, output: Path) -> None:
    """Render a 1200×675 card from the downloaded retailer image."""
    canvas = Image.new("RGB", (1200, 675), "#f2f0e9")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((595, 0, 1200, 675), fill="#111d2a")
    draw.rectangle((595, 0, 1200, 12), fill="#fc7048")
    with Image.open(product_image) as source:
        figure = ImageOps.contain(_crop_black_letterbox(source.convert("RGB")), (530, 575), Image.Resampling.LANCZOS)
        canvas.paste(figure, ((595 - figure.width) // 2, (645 - figure.height) // 2))
    draw = ImageDraw.Draw(canvas)
    draw.text((650, 48), "FIGURES", font=_font(31, True), fill="#f5f2e9")
    draw.text((822, 48), "RADAR", font=_font(31, True), fill="#fc7048")
    draw.rounded_rectangle((650, 113, 940, 157), radius=22, fill="#fc7048")
    draw.text((674, 121), "FIGURE DEAL", font=_font(22, True), fill="#111d2a")
    title = _display_title(deal["figure_name"])
    title_font = _font(39, True)
    lines = _fit_lines(draw, title, title_font, 495, 3)
    if lines[-1].endswith("…"):
        title_font = _font(32, True)
        lines = _fit_lines(draw, title, title_font, 495, 3)
    for index, line in enumerate(lines):
        draw.text((650, 190 + index * 52), line, font=title_font, fill="#ffffff")
    retailer = deal["retailer"]
    draw.text((650, 389), retailer, font=_font(23), fill="#aebccc")
    current = _money(Decimal(deal["current_price"]), deal["currency"])
    regular = deal.get("regular_price")
    if regular is not None:
        old = _money(Decimal(regular), deal["currency"])
        draw.text((650, 453), old, font=_font(28), fill="#9ba8b6")
        old_width = draw.textbbox((0, 0), old, font=_font(28))[2]
        draw.line((650, 474, 650 + old_width, 474), fill="#9ba8b6", width=2)
    draw.text((650, 497), current, font=_font(61, True), fill="#ffffff")
    discount = deal.get("discount_percent")
    if discount is not None and Decimal(discount) > 0:
        label = f"-{Decimal(discount):.0f}%"
        draw.rounded_rectangle((965, 506, 1135, 564), radius=19, fill="#fc7048")
        draw.text((986, 515), label, font=_font(31, True), fill="#111d2a")
    else:
        draw.text((650, 577), "DEAL DETECTED", font=_font(20, True), fill="#fc7048")
    draw.line((650, 615, 1135, 615), fill="#52606e", width=1)
    draw.text((650, 631), "Powered by NekoPrice", font=_font(17), fill="#aebccc")
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, format="JPEG", quality=91, optimize=True)
