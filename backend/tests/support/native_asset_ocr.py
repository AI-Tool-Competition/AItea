"""Historical fixture builders for version/migration and preview contracts."""

import io

from app.contexts.knowledge.infrastructure.parsing import parsing as parsing
from app.contexts.knowledge.infrastructure.parsing.docx_parsing import parse_docx
from app.contexts.knowledge.infrastructure.providers import vision as vision
from PIL import Image

from support.docx_parsing import _p, _package


def raster(*, format="PNG", frames=False, size=(8, 4)):
    output = io.BytesIO()
    image = Image.new("RGB", size, "white")
    options = {"save_all": True, "append_images": [Image.new("RGB", size, "black")]} if frames else {}
    image.save(output, format=format, **options)
    return output.getvalue()


def docx(raw=None, *, count=1):
    raw = raster() if raw is None else raw
    body, parts, relationships = _p("Native text before images"), {}, []
    for index in range(count):
        body += (
            '<w:p><w:r><w:drawing><a:blip xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            f'r:embed="im{index}"/></w:drawing></w:r></w:p>'
        )
        parts[f"word/media/im{index}.png"] = raw
        relationships.append((f"im{index}", "image", f"media/im{index}.png", None))
    content = _package(body, parts=parts, relations=relationships)
    return content, parse_docx(content)
