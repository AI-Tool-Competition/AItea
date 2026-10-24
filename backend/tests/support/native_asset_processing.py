"""Historical fixture builders for version/migration and preview contracts."""

import io

from PIL import Image

from support.docx_parsing import _p, _package


def image_document():
    image = io.BytesIO()
    Image.new("RGB", (60, 40), "white").save(image, format="PNG")
    return _package(
        _p("Original native text")
        + '<w:p><w:r><w:drawing><x:blip xmlns:x="urn:picture" r:embed="pic"/></w:drawing></w:r></w:p>',
        parts={"word/media/picture.png": image.getvalue()},
        relations=[("pic", "image", "media/picture.png", None)],
    )


def transcript():
    return {
        "text": "All visible printed text in the embedded figure.",
        "complete": True,
        "is_blank": False,
        "uncertain_spans": [],
        "model": "verified-test-provider",
        "requested_model": "test-model",
        "prompt_version": "test-image-v1",
        "review_status": "unreviewed",
        "finish_reason": "stop",
    }
