"""
Extracts all tables and figures from PDF documents, with the associated captions/
headings/footnotes, as images.
Adapted from TF-ID model https://github.com/ai8hyf/TF-ID

Vendored from MERMaid into surf_extractor.
"""

import os
from unittest.mock import patch
from typing import Union
from pathlib import Path
import platform

LARGE_MODEL_ID = "shixuanleong/visualheist-large"
BASE_MODEL_ID = "shixuanleong/visualheist-base"
LARGE_SAFETENSORS_PATH = "https://huggingface.co/shixuanleong/visualheist-large/resolve/main/model.safetensors"
BASE_SAFETENSORS_PATH = "https://huggingface.co/shixuanleong/visualheist-base/resolve/main/model.safetensors"


def fixed_get_imports(filename: Union[str, os.PathLike]) -> list[str]:
    """Workaround to remove flash_attn from imports."""
    from transformers.dynamic_module_utils import get_imports
    if not str(filename).endswith("modeling_florence2.py"):
        return get_imports(filename)
    imports = get_imports(filename)
    if "flash_attn" in imports:
        imports.remove("flash_attn")
    return imports


def _pdf_to_image(pdf_path):
    """Converts a pdf into a list of images."""
    from pdf2image import convert_from_path
    system = platform.system()
    if system == "Windows":
        poppler_path = os.environ.get("POPPLER_PATH")
        if poppler_path is None:
            raise RuntimeError("Please set the POPPLER_PATH environment variable to your Poppler binary directory.")
        return convert_from_path(str(pdf_path), poppler_path=poppler_path)
    return convert_from_path(str(pdf_path))


def _tf_id_detection(image, model, processor):
    """Performs table and figure identification using model and processor on image."""
    prompt = "<OD>"
    inputs = processor(text=prompt, images=image, return_tensors="pt")

    generated_ids = model.generate(
        input_ids=inputs["input_ids"],
        pixel_values=inputs["pixel_values"],
        max_new_tokens=1024,
        do_sample=False,
        num_beams=3
    )

    generated_text = processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
    annotation = processor.post_process_generation(
        generated_text, task="<OD>", image_size=(image.width, image.height)
    )

    return annotation["<OD>"]


def _save_image_from_bbox(image, annotation, image_counter, output_dir, pdf_name):
    """Saves cropped regions denoted from annotation in image to output_dir."""
    output_dir = Path(output_dir)
    for counter, bbox in enumerate(annotation['bboxes']):
        x1, y1, x2, y2 = bbox
        cropped_image = image.crop((x1, y1, x2, y2))
        image_path = output_dir / f"{pdf_name}_image_{image_counter + counter + 1}.png"
        cropped_image.save(image_path)
    return len(annotation["bboxes"]) + image_counter


def _create_model(model_id):
    """Initializes model used for segmenting tables and figures."""
    from transformers import AutoProcessor, AutoModelForCausalLM
    with patch("transformers.dynamic_module_utils.get_imports", fixed_get_imports):
        model = AutoModelForCausalLM.from_pretrained(model_id, trust_remote_code=True)
        processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
    return model, processor


def _pdf_to_figures_and_tables(pdf_path, output_dir, large_model):
    """Takes a single pdf and runs the model on it to extract tables and figures."""
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_name = pdf_path.stem
    images = _pdf_to_image(pdf_path)
    print(f"\nPDF {pdf_name} is loaded.")

    if large_model:
        model, processor = _create_model(LARGE_MODEL_ID)
    else:
        model, processor = _create_model(BASE_MODEL_ID)
        print('model and processor is loaded')

    image_counter = 0
    for i, image in enumerate(images):
        annotation = _tf_id_detection(image, model, processor)
        image_counter = _save_image_from_bbox(image, annotation, image_counter, output_dir, pdf_name)
        print(f"Page {i} saved. Number of objects: {len(annotation['bboxes'])}")
    print(f"All extracted images from {pdf_name} are saved")
    print("=====================================")


def batch_pdf_to_figures_and_tables(input_dir, output_dir=None, large_model=False):
    """Takes a directory of pdfs via input_dir and saves tables and figures in output_dir."""
    input_dir = Path(input_dir)
    output_dir = Path(output_dir) if output_dir else input_dir / "extracted_images"
    output_dir.mkdir(parents=True, exist_ok=True)

    for file in input_dir.iterdir():
        if file.suffix.lower() != ".pdf":
            print(f"ERROR: {file.name} is not a PDF. Moving on to next file.")
            continue
        try:
            _pdf_to_figures_and_tables(file, output_dir, large_model)
        except Exception as e:
            print(e)
            print(f"ERROR: Failed to process {file.name}:{e}. Moving on to next file.\n")
            continue
