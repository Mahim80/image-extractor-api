import fitz  # PyMuPDF
from fastapi import FastAPI, UploadFile, File
import io, base64

app = FastAPI()

@app.post("/extract-images")
async def extract_images(file: UploadFile = File(...)):
    pdf_bytes = await file.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0] 
    
    # ১. ইউজার ফটো খুঁজে বের করা (অ্যাঙ্কর পয়েন্ট)
    all_imgs = []
    for img_info in page.get_images(full=True):
        xref = img_info[0]
        base_image = doc.extract_image(xref)
        rects = page.get_image_rects(xref)
        if rects:
            r = rects[0]
            if r.width > 45 and r.height > 45:
                all_imgs.append({"bytes": base_image["image"], "rect": r})

    if not all_imgs:
        return {"success": False, "message": "User photo not found"}

    # ইউজার ফটো নির্ধারণ (সবচেয়ে উপরে ও ডানে থাকা ছবি)
    all_imgs = sorted(all_imgs, key=lambda x: (x['rect'].y0, -x['rect'].x0))
    user_photo_info = all_imgs[0]
    user_photo = base64.b64encode(user_photo_info['bytes']).decode('utf-8')
    p = user_photo_info['rect'] # ফটোর পজিশন

    # ২. ড্রয়িং বক্স খোঁজা (ছোট বা বড় সব বক্স ধরবে)
    drawings = page.get_drawings()
    potential_boxes = []
    
    for d in drawings:
        r = d["rect"]
        
        # লজিক: বক্সটি ফটোর নিচে হতে হবে এবং ফটোর সোজাসুজি ডানে/বামে থাকতে হবে
        # বক্সের কেন্দ্র এবং ফটোর কেন্দ্রের দূরত্বের পার্থক্য কম হতে হবে
        photo_center_x = (p.x0 + p.x1) / 2
        rect_center_x = (r.x0 + r.x1) / 2
        
        # বক্সটি ফটোর নিচে ১০০ পিক্সেলের মধ্যে থাকতে হবে
        if r.y0 > p.y1 - 5 and r.y0 < p.y1 + 100:
            if abs(photo_center_x - rect_center_x) < 40: # ভার্টিক্যাল অ্যালাইনমেন্ট চেক
                # বক্সের মিনিমাম সাইজ অনেক কমিয়ে দেওয়া হয়েছে যাতে ছোট বক্সও ধরা পড়ে
                if r.width > 50 and r.height > 15: 
                    potential_boxes.append(r)

    target_rect = None

    if potential_boxes:
        # ফটোর সবচেয়ে কাছে যে বক্সটি আছে সেটিই সিগনেচার বক্স
        # এটি ছোট বক্স বা বড় বক্স যাই হোক না কেন ধরবে
        final_box = sorted(potential_boxes, key=lambda b: b.y0)[0]
        
        # ৩. বক্সের ২ পিক্সেল ভেতর থেকে ক্রপ করা
        target_rect = fitz.Rect(
            final_box.x0 + 2, 
            final_box.y0 + 2, 
            final_box.x1 - 2, 
            final_box.y1 - 2
        )
    else:
        # ৪. যদি কোনো ড্রয়িং বক্স না পায়, তবে টেক্সট চেক করবে
        # "License" লেখাটি কোথায় আছে খুঁজে তার উপর পর্যন্ত কাটবে
        text_blocks = page.get_text("blocks")
        license_y = p.y1 + 75 # ডিফল্ট লিমিট
        
        for b in text_blocks:
            if "License" in b[4] or "Document" in b[4]:
                license_y = b[1] - 2 # লেখার ২ পিক্সেল উপরে থামবে
                break
        
        target_rect = fitz.Rect(p.x0, p.y1 + 2, p.x1, license_y)

    # ৫. রেন্ডারিং
    pix = page.get_pixmap(
        clip=target_rect, 
        matrix=fitz.Matrix(5, 5), # ক্লিয়ার রেজাল্টের জন্য ৫ গুণ জুম
        colorspace=fitz.csRGB, 
        alpha=False
    )
    
    img_data = pix.tobytes("png")
    signature = base64.b64encode(img_data).decode('utf-8')

    return {
        "success": True,
        "user_photo": user_photo,
        "signature": signature
    }
