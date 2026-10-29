"""Local image pagination in the existing resource-bounded document worker."""

import base64
import io
import json
import warnings

from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter, Transformation

from app.contexts.documents.infrastructure.parsers.document_parser import _run_bounded

MAX_PIXELS = 40_000_000
MAX_BYTES = 20 * 1024 * 1024
MAX_SLICES = 64
MAX_PAGES = 32
A4 = (595.276, 841.890)


def convert_image(content, options, *, timeout=20):
    if not 0 < len(content) <= MAX_BYTES:
        raise ValueError("图片不能为空或超过 20 MB")
    return _run_bounded(_worker, (content, options), min(20, timeout))


def _worker(send, content, options):
    try:
        result = paginate(content, **options)
        send.send_bytes(json.dumps({"result": result}).encode())
    except Exception:
        # Do not expose native decoder details or user image contents.
        send.send_bytes(json.dumps({"error": "图片无法转换，请检查图片格式、尺寸或切片参数。"}).encode())
    finally:
        send.close()


def paginate(content, *, slice_height=2000, overlap=200, cols=2, rows=2, layout="grid"):
    if (
        not 0 < len(content) <= MAX_BYTES
        or any(type(n) is not int for n in (slice_height, overlap, cols, rows))
        or not 400 <= slice_height <= 4000
        or not 0 <= overlap <= slice_height // 2
        or not 1 <= cols <= 3
        or not 1 <= rows <= 4
        or layout not in ("grid", "column")
    ):
        raise ValueError("图片大小或分页参数超出限制")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(content)) as original:
            if original.format not in ("PNG", "JPEG", "WEBP") or getattr(original, "n_frames", 1) != 1:
                raise ValueError("只支持单帧 PNG、JPEG、WebP")
            if original.width * original.height > MAX_PIXELS or max(original.size) > 200_000:
                raise ValueError("图片尺寸超出限制")
            image = ImageOps.exif_transpose(original).convert("RGBA")
    # Composite transparency onto white without changing the uploaded original.
    background = Image.new("RGB", image.size, "white")
    background.paste(image, mask=image.getchannel("A"))
    image.close()
    bounds = []
    start = 0
    while start < background.height:
        end = min(start + slice_height, background.height)
        bounds.append((start, end))
        if end == background.height:
            break
        start = end - overlap
        if len(bounds) >= MAX_SLICES:
            background.close()
            raise ValueError("切片超过 64 张，请增大每片高度")
    per_page = cols * rows
    page_count = (len(bounds) + per_page - 1) // per_page
    if page_count > MAX_PAGES:
        background.close()
        raise ValueError("PDF 超过 32 页，请调整分页")
    writer = PdfWriter()
    margin, gap = 24, 10
    cell_w = (A4[0] - 2 * margin - (cols - 1) * gap) / cols
    cell_h = (A4[1] - 2 * margin - (rows - 1) * gap) / rows
    try:
        for index, (top, bottom) in enumerate(bounds):
            cell = index % per_page
            if cell == 0:
                page = writer.add_blank_page(*A4)
            row, col = divmod(cell, cols) if layout == "grid" else (cell % rows, cell // rows)
            with background.crop((0, top, background.width, bottom)) as crop:
                buffer = io.BytesIO()
                crop.save(buffer, format="PDF", resolution=72)
            source = PdfReader(buffer).pages[0]
            scale = min(cell_w / float(source.mediabox.width), cell_h / float(source.mediabox.height))
            x = margin + col * (cell_w + gap) + (cell_w - float(source.mediabox.width) * scale) / 2
            y = A4[1] - margin - row * (cell_h + gap) - float(source.mediabox.height) * scale
            page.merge_transformed_page(source, Transformation().scale(scale).translate(x, y))
        output = io.BytesIO()
        writer.write(output)
        data = output.getvalue()
        if len(data) > MAX_BYTES or len(PdfReader(io.BytesIO(data)).pages) != page_count:
            raise ValueError("PDF 超出大小限制或页数校验失败")
        return {
            "pdf_base64": base64.b64encode(data).decode(),
            "page_count": page_count,
            "slice_count": len(bounds),
            "slice_bounds": bounds,
            "width": background.width,
            "height": background.height,
            "layout": layout,
        }
    finally:
        background.close()
        writer.close()
