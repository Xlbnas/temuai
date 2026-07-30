"""Deterministic Pillow annotation and reference-board rendering."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from src.studio.models import Asset, DetailRegion


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for font_name in ("NotoSansCJK-Bold.ttc", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(font_name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_annotations(source: Path, destination: Path, regions: list[DetailRegion]) -> Path:
    """Render red callouts without ever modifying the uploaded original."""
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    font = _font(max(12, min(width, height) // 28))
    for number, region in enumerate(regions, start=1):
        if region.normalized_bbox is None:
            continue
        box = region.normalized_bbox
        xy = (
            int(box.x * width),
            int(box.y * height),
            int((box.x + box.width) * width),
            int((box.y + box.height) * height),
        )
        draw.rounded_rectangle(
            xy, radius=max(4, width // 100), outline="#e11d48", width=max(2, width // 220)
        )
        label = f"{number}. {region.label.effective_value}"[:50]
        label_box = draw.textbbox((0, 0), label, font=font)
        label_width = label_box[2] - label_box[0] + 10
        label_height = label_box[3] - label_box[1] + 8
        tx = min(max(0, xy[0]), max(0, width - label_width))
        ty = max(0, xy[1] - label_height - 5)
        draw.line(
            (xy[0], xy[1], tx + 5, ty + label_height), fill="#e11d48", width=max(2, width // 260)
        )
        draw.rounded_rectangle(
            (tx, ty, tx + label_width, ty + label_height), radius=4, fill="#e11d48"
        )
        draw.text((tx + 5, ty + 4), label, fill="white", font=font)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG")
    return destination


def render_detail_board(items: list[tuple[Asset, Path]], destination: Path) -> Path:
    """Make an offline numbered detail board, including the empty-board case."""
    cell_w, cell_h = 320, 380
    rows = max(1, (len(items) + 1) // 2)
    board = Image.new("RGB", (cell_w * 2, cell_h * rows), "white")
    draw = ImageDraw.Draw(board)
    font = _font(20)
    for index, (asset, path) in enumerate(items, start=1):
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
        image.thumbnail((cell_w - 24, cell_h - 60))
        x = ((index - 1) % 2) * cell_w + (cell_w - image.width) // 2
        y = ((index - 1) // 2) * cell_h + 32
        board.paste(image, (x, y))
        draw.text(
            (x, 6 + ((index - 1) // 2) * cell_h),
            f"{index}. {asset.original_filename[:35]}",
            fill="black",
            font=font,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    board.save(destination, format="PNG")
    return destination
