import base64
import io
import re
from datetime import datetime
from typing import Any, Optional

import fitz  # PyMuPDF
from fastapi import FastAPI, File, UploadFile

app = FastAPI(title="Image and PDF Data Extractor API", version="2.0.0")


# Common broken Bengali word fixes observed in NID PDF text extraction.
BENGALI_REPLACEMENTS = {
    "খাতু ন": "খাতুন",
    "আব্দু ল": "আব্দুল",
    "বর্ম ন": "বর্মন",
    "হোসে ন": "হোসেন",
    "রহমা ন": "রহমান",
    "বেগ ম": "বেগম",
    "আল ম": "আলম",
    "উদ্দি ন": "উদ্দিন",
    "ইসলাম ম": "ইসলাম",
    "আহমে দ": "আহমেদ",
    "আলী ম": "আলীম",
    "সরকা র": "সরকার",
    "মিঞা য়": "মিঞায়",
    " মি য়া": " মিয়া",
    " মিঞা": " মিঞা",
    "তালি ক": "তালিকা",
    "কু লসুম": "কুলসুম",
    "পুকু র": "পুকুর",
    "আবদু ল": "আবদুল",
    "সিফাতু ন": "সিফাতুন",
    "নতু ন": "নতুন",
    "সর্দা র": "সর্দার",
    "আরসাফু ল": "আরসাফুল",
    "ISL AM": "ISLAM",
    "রহিদু ল": "রহিদুল",
}


def clean_bengali_text(text: Optional[str]) -> str:
    if not text:
        return ""
    for old, new in BENGALI_REPLACEMENTS.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()


def convert_to_bangla(number: Any) -> str:
    return str(number).translate(str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯"))


def normalize(text: str) -> str:
    return re.sub(r"[ \t\r\n]+", " ", text or " ").strip()


def extract_between(text: str, start: str, end: Optional[str] = None) -> str:
    """Extract text between labels, tolerating line breaks and extra spaces."""
    if not text:
        return ""
    start_pattern = re.escape(start).replace(r"\ ", r"\s*")
    if end:
        end_pattern = re.escape(end).replace(r"\ ", r"\s*")
        match = re.search(start_pattern + r"\s*(.*?)\s*" + end_pattern, text, re.I | re.S)
    else:
        match = re.search(start_pattern + r"\s*(.*?)(?:\n|$)", text, re.I | re.S)
    return normalize(match.group(1)) if match else ""


def first_nonempty(*values: str) -> str:
    return next((normalize(v) for v in values if normalize(v)), "")


def extract_text_fields(text: str) -> dict[str, str]:
    # Text from this kind of PDF may place labels and values on separate lines.
    fields = {
        "nationalId": extract_between(text, "National ID", "Pin"),
        "pin": extract_between(text, "Pin", "Status"),
        "status": extract_between(text, "Status", "Afis Status"),
        "nameBangla": extract_between(text, "Name(Bangla)", "Name(English)"),
        "nameEnglish": extract_between(text, "Name(English)", "Date of Birth"),
        "dateOfBirth": extract_between(text, "Date of Birth", "Birth Place"),
        "birthPlace": extract_between(text, "Birth Place", "Birth Other"),
        "fatherName": extract_between(text, "Father Name", "Mother Name"),
        "motherName": extract_between(text, "Mother Name", "Spouse Name"),
        "spouseName": extract_between(text, "Spouse Name", "Gender"),
        "gender": extract_between(text, "Gender", "Marital"),
        "marital": extract_between(text, "Marital", "Occupation"),
        "occupation": extract_between(text, "Occupation", "Disability"),
        "religion": extract_between(text, "Religion", "Religion Other"),
        "bloodGroup": extract_between(text, "Blood Group", "TIN"),
        "laptopId": extract_between(text, "Laptop ID", "NID Father"),
    }

    # A few PDFs flatten values into lines, so use label-based fallbacks.
    lines = [normalize(line) for line in text.splitlines() if normalize(line)]
    for index, line in enumerate(lines):
        for label, key in [
            ("National ID", "nationalId"), ("Pin", "pin"), ("Name(Bangla)", "nameBangla"),
            ("Name(English)", "nameEnglish"), ("Date of Birth", "dateOfBirth"),
            ("Father Name", "fatherName"), ("Mother Name", "motherName"),
            ("Gender", "gender"), ("Religion", "religion"), ("Blood Group", "bloodGroup"),
        ]:
            if line.lower().startswith(label.lower()) and not fields[key]:
                value = line[len(label):].strip(" :\t")
                fields[key] = value or (lines[index + 1] if index + 1 < len(lines) else "")

    fields["nameBangla"] = clean_bengali_text(fields["nameBangla"])
    fields["fatherName"] = clean_bengali_text(fields["fatherName"])
    fields["motherName"] = clean_bengali_text(fields["motherName"])
    fields["nameEnglish"] = fields["nameEnglish"].upper()
    fields["gender"] = fields["gender"].upper()
    fields["bloodGroup"] = fields["bloodGroup"].upper()
    return fields


def extract_images(doc: fitz.Document) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Find portrait and signature across every page of a 1–3 page PDF."""
    candidates: list[dict[str, Any]] = []
    for page_number, page in enumerate(doc):
        for image_info in page.get_images(full=True):
            xref = image_info[0]
            base_image = doc.extract_image(xref)
            rects = page.get_image_rects(xref)
            if rects:
                rect = rects[0]
                if rect.width > 45 and rect.height > 45:
                    candidates.append({
                        "bytes": base_image["image"],
                        "rect": rect,
                        "page_number": page_number,
                    })

    if not candidates:
        return None, None

    # Usually the portrait is the largest qualifying image. This works even
    # when the portrait is on page 2 or 3, instead of assuming page 1.
    portrait = max(candidates, key=lambda item: item["rect"].width * item["rect"].height)
    signature = None

    # Look for a signature box on the same page and immediately below portrait.
    page = doc[portrait["page_number"]]
    photo = portrait["rect"]
    photo_center_x = (photo.x0 + photo.x1) / 2
    boxes = []
    for drawing in page.get_drawings():
        rect = drawing["rect"]
        rect_center_x = (rect.x0 + rect.x1) / 2
        if photo.y1 - 5 < rect.y0 < photo.y1 + 100 and abs(photo_center_x - rect_center_x) < 40:
            if rect.width > 50 and rect.height > 15:
                boxes.append(rect)
    if boxes:
        target = sorted(boxes, key=lambda rect: rect.y0)[0]
        crop = fitz.Rect(target.x0 + 2, target.y0 + 2, target.x1 - 2, target.y1 - 2)
        pix = page.get_pixmap(clip=crop, matrix=fitz.Matrix(5, 5), colorspace=fitz.csRGB, alpha=False)
        signature = {"bytes": pix.tobytes("png"), "rect": target, "page_number": portrait["page_number"]}

    # Fallback: if no drawing box exists, choose a smaller image below the
    # portrait on the same page as the signature candidate.
    if signature is None:
        same_page = [item for item in candidates if item["page_number"] == portrait["page_number"] and item is not portrait]
        below = [item for item in same_page if item["rect"].y0 >= photo.y1]
        if below:
            signature = min(below, key=lambda item: item["rect"].width * item["rect"].height)

    return portrait, signature


def image_to_base64(image: Optional[dict[str, Any]]) -> Optional[str]:
    return base64.b64encode(image["bytes"]).decode("ascii") if image else None


@app.get("/")
def root() -> dict[str, Any]:
    return {"success": True, "service": "image-extractor-api", "docs": "/docs"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/extract-images")
async def extract_images_api(file: UploadFile = File(...)) -> dict[str, Any]:
    pdf_bytes = await file.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    portrait, signature = extract_images(doc)
    if not portrait:
        return {"success": False, "message": "User photo not found"}
    return {
        "success": True,
        "user_photo": image_to_base64(portrait),
        "signature": image_to_base64(signature),
    }


@app.post("/extract-data")
async def extract_data_api(file: UploadFile = File(...)) -> dict[str, Any]:
    """Extract NID-like text fields plus portrait/signature from the first PDF page."""
    pdf_bytes = await file.read()
    if not pdf_bytes:
        return {"success": False, "message": "Empty PDF file"}
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if doc.page_count == 0:
            return {"success": False, "message": "PDF has no pages"}
        text = "\n".join(page.get_text("text") for page in doc)
        fields = extract_text_fields(text)
        portrait, signature = extract_images(doc)
        return {
            "code": 200,
            "success": True,
            "message": "Data fetched successfully",
            "data": fields,
            "images": {
                "user_photo_base64": image_to_base64(portrait),
                "signature_base64": image_to_base64(signature),
            },
        }
    except Exception as exc:
        return {"success": False, "message": "PDF parsing error", "details": str(exc)}
