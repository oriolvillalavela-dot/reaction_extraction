"""
MERMaid wrapper — text extraction (pdfplumber/PyMuPDF) + optional VisualHeist
image extraction. VisualHeist is vendored in backend/vendor/visualheist and
requires heavy ML dependencies (torch, transformers); install them via the
VisualHeist section in requirements.txt. Falls back to text-only mode gracefully.
"""

import os
import base64
import logging
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy import – VisualHeist pulls in heavy ML deps (transformers, torch).
# Cached so the warning fires only once; returns None if deps are missing.
# ---------------------------------------------------------------------------

_visualheist_fn = None
_visualheist_checked = False


def _try_import_visualheist():
    """Return the VisualHeist batch function, or None (checked once, result cached)."""
    global _visualheist_fn, _visualheist_checked
    if _visualheist_checked:
        return _visualheist_fn
    _visualheist_checked = True
    missing = []
    for pkg in ("pdf2image", "transformers", "torch"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        logger.warning(
            "VisualHeist not available: missing packages %s – falling back to text-only mode. "
            "Uncomment the VisualHeist section in requirements.txt to enable image extraction.",
            ", ".join(missing),
        )
        return None
    try:
        from backend.vendor.visualheist.methods_visualheist import batch_pdf_to_figures_and_tables
        _visualheist_fn = batch_pdf_to_figures_and_tables
        logger.info("VisualHeist loaded successfully.")
    except Exception as exc:
        logger.warning("VisualHeist failed to load: %s – falling back to text-only mode.", exc)
    return _visualheist_fn


def _try_import_pdfplumber():
    try:
        import pdfplumber
        return pdfplumber
    except ImportError:
        return None


def _try_import_pymupdf():
    try:
        import fitz  # PyMuPDF
        return fitz
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def extract_text_from_pdf(pdf_path: str) -> str:
    """
    Extract plain text from a PDF file.
    Tries pdfplumber first, then PyMuPDF, then returns empty string.
    """
    pdf_path = Path(pdf_path)

    pdfplumber = _try_import_pdfplumber()
    if pdfplumber is not None:
        try:
            with pdfplumber.open(str(pdf_path)) as pdf:
                pages = []
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text() or ""
                    # Also extract tables as markdown-ish text
                    tables = page.extract_tables() or []
                    table_text = ""
                    for tbl in tables:
                        for row in tbl:
                            row_str = "\t".join(str(c) if c else "" for c in row)
                            table_text += row_str + "\n"
                    pages.append(f"=== Page {i+1} ===\n{text}\n{table_text}")
            return "\n\n".join(pages)
        except Exception as exc:
            logger.warning("pdfplumber failed for %s: %s", pdf_path, exc)

    fitz = _try_import_pymupdf()
    if fitz is not None:
        try:
            doc = fitz.open(str(pdf_path))
            pages = []
            for i, page in enumerate(doc):
                text = page.get_text()
                pages.append(f"=== Page {i+1} ===\n{text}")
            doc.close()
            return "\n\n".join(pages)
        except Exception as exc:
            logger.warning("PyMuPDF failed for %s: %s", pdf_path, exc)

    logger.error("No PDF text-extraction library available.")
    return ""


def extract_images_from_pdf(pdf_path: str, output_dir: str, large_model: bool = False) -> list[str]:
    """
    Use MERMaid VisualHeist to extract tables & figures from a PDF as cropped PNG images.

    Returns a list of absolute paths to the saved image files.
    Falls back to empty list if VisualHeist is unavailable.
    """
    batch_fn = _try_import_visualheist()
    if batch_fn is None:
        logger.warning("VisualHeist unavailable – skipping image extraction for %s", pdf_path)
        return []

    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # VisualHeist expects a *directory* of PDFs; create a temp directory
    # containing only our single PDF.
    with tempfile.TemporaryDirectory() as tmp_pdf_dir:
        import shutil
        tmp_pdf_path = Path(tmp_pdf_dir) / pdf_path.name
        shutil.copy2(str(pdf_path), str(tmp_pdf_path))

        try:
            batch_fn(input_dir=tmp_pdf_dir, output_dir=str(output_dir), large_model=large_model)
        except Exception as exc:
            logger.error("VisualHeist raised an exception: %s", exc, exc_info=True)
            return []

    # Collect all PNG/JPG files saved by VisualHeist
    image_paths = sorted(
        p for p in output_dir.rglob("*")
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    )
    return [str(p) for p in image_paths]


def images_to_base64(image_paths: list[str]) -> list[dict]:
    """
    Convert a list of image file paths into base64-encoded dicts suitable
    for passing to a multimodal LLM (OpenAI / Gemini via compatible API).

    Returns a list of:
        {"mime_type": "image/png", "data": "<base64>", "path": "<path>"}
    """
    result = []
    for path in image_paths:
        p = Path(path)
        if not p.exists():
            continue
        suffix = p.suffix.lower()
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(suffix, "image/png")
        try:
            data = base64.b64encode(p.read_bytes()).decode()
            result.append({"mime_type": mime, "data": data, "path": str(p)})
        except Exception as exc:
            logger.warning("Could not encode image %s: %s", path, exc)
    return result


def parse_pdfs(
    main_pdf_path: str,
    si_pdf_path: Optional[str],
    image_output_dir: str,
    use_visualheist: bool = True,
    large_model: bool = False,
) -> dict:
    """
    Top-level parser: extracts text + images from main paper and SI.

    Returns:
        {
            "main_text": str,
            "si_text": str,
            "images": list[dict],   # base64-encoded image dicts
            "image_paths": list[str]
        }
    """
    logger.info("Extracting text from main PDF: %s", main_pdf_path)
    main_text = extract_text_from_pdf(main_pdf_path)

    si_text = ""
    if si_pdf_path:
        logger.info("Extracting text from SI PDF: %s", si_pdf_path)
        si_text = extract_text_from_pdf(si_pdf_path)

    image_paths: list[str] = []
    if use_visualheist:
        logger.info("Running VisualHeist on main PDF…")
        image_paths.extend(
            extract_images_from_pdf(main_pdf_path, image_output_dir, large_model)
        )
        if si_pdf_path:
            logger.info("Running VisualHeist on SI PDF…")
            image_paths.extend(
                extract_images_from_pdf(si_pdf_path, image_output_dir, large_model)
            )

    images = images_to_base64(image_paths)
    logger.info(
        "Parsing complete – main_text=%d chars, si_text=%d chars, images=%d",
        len(main_text), len(si_text), len(images),
    )

    return {
        "main_text": main_text,
        "si_text": si_text,
        "images": images,
        "image_paths": image_paths,
    }
