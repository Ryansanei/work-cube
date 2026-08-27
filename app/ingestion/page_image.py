"""Renders a document's first page to a PNG for the triage UI's bounding-box
overlay — the browser draws overlay boxes in CSS pixels, so the page needs
to be a raster image, not a PDF the browser would need its own renderer for.

Docling's bbox coordinates are in PDF points, BOTTOMLEFT origin (y grows
upward from the page bottom). `bbox_to_css_pixels` converts a box to CSS's
top-left-origin pixel space at the same zoom this module renders at.
"""

import fitz

PAGE_WIDTH_PT = 612.0  # reportlab letter size — fixed, since this project renders its own PDFs
PAGE_HEIGHT_PT = 792.0
DEFAULT_ZOOM = 2.0


def render_page_png(pdf_path: str, zoom: float = DEFAULT_ZOOM) -> bytes:
    doc = fitz.open(pdf_path)
    try:
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        return pix.tobytes("png")
    finally:
        doc.close()


def bbox_to_css_pixels(
    bbox: tuple[float, float, float, float], zoom: float = DEFAULT_ZOOM
) -> dict[str, float]:
    left, top, right, bottom = bbox
    return {
        "left": left * zoom,
        "top": (PAGE_HEIGHT_PT - top) * zoom,
        "width": (right - left) * zoom,
        "height": (top - bottom) * zoom,
    }
