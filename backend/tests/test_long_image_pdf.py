import base64
import io

import pytest
import pypdfium2
from PIL import Image
from pypdf import PdfReader

from app.contexts.documents.infrastructure.long_image_pdf import paginate


def picture(height=1600):
    image = Image.new("RGB", (200, height), "white")
    for index, color in enumerate(("red", "green", "blue", "yellow")):
        image.paste(color, (0, index * 400, 200, min((index + 1) * 400, height)))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


@pytest.mark.parametrize("layout,order", [("grid", (0, 1, 2, 3)), ("column", (0, 2, 1, 3))])
def test_pdf_render_preserves_physical_reading_order(layout, order):
    result = paginate(picture(), slice_height=400, overlap=0, layout=layout)
    pdf = base64.b64decode(result["pdf_base64"])
    assert result["slice_bounds"] == [(0, 400), (400, 800), (800, 1200), (1200, 1600)]
    assert result["page_count"] == 1
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 1 and len(reader.pages[0].images) == 4
    with pypdfium2.PdfDocument(pdf) as document:
        page = document[0]
        bitmap = page.render()
        rendered = bitmap.to_pil().convert("RGB")
        colors = [(255, 0, 0), (0, 128, 0), (0, 0, 255), (255, 255, 0)]
        for (x, y), index in zip(((160, 150), (430, 150), (160, 550), (430, 550)), order, strict=True):
            actual = rendered.getpixel((x, y))
            assert all(abs(a - b) < 15 for a, b in zip(actual, colors[index], strict=True))
        rendered.close()
        bitmap.close()
        page.close()


def test_overlap_covers_the_last_pixel_without_redundant_tail_slice():
    result = paginate(picture(), slice_height=700, overlap=200, cols=1, rows=1)
    assert result["slice_bounds"] == [(0, 700), (500, 1200), (1000, 1600)]
    assert result["slice_count"] == result["page_count"] == 3


@pytest.mark.parametrize(
    "options",
    [
        {"overlap": 2000},
        {"cols": 0},
        {"rows": 5},
        {"layout": "arbitrary"},
        {"slice_height": True},
    ],
)
def test_invalid_pagination_is_rejected(options):
    with pytest.raises(ValueError):
        paginate(picture(), **options)


def test_invalid_image_and_excessive_slices_are_rejected():
    with pytest.raises(Exception):
        paginate(b"not an image")
    with pytest.raises(ValueError, match="64"):
        paginate(picture(40000), slice_height=400, overlap=0)
