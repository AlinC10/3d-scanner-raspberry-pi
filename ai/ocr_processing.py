import pytesseract
import fitz 
from PIL import Image, ImageEnhance
import io
from langchain_core.documents import Document

pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

def extract_text_ocr(file_path):
    """Extracts text from a scanned PDF using PyMuPDF and Tesseract with preprocessing."""
    print(f"[OCR] Scanned document detected. Starting OCR processing for: {file_path}...")
    documents = []
    try:
        pdf_doc = fitz.open(file_path)
        for i, page in enumerate(pdf_doc):
            pix = page.get_pixmap(dpi=300) 
            img_bytes = pix.tobytes("png")
            image = Image.open(io.BytesIO(img_bytes))
            image = image.convert('L')
            enhancer = ImageEnhance.Contrast(image)
            image = enhancer.enhance(2.0) 
            custom_config = r'--oem 3 --psm 4'
            text = pytesseract.image_to_string(image, lang='ron+eng', config=custom_config)
            cleaned_text = "\n".join([line for line in text.splitlines() if line.strip()])
            doc = Document(
                page_content=cleaned_text,
                metadata={"source": file_path, "page": i}
            )
            documents.append(doc)
            
        print(f"[OCR] Success! Extracted text from {len(documents)} pages.")
    except Exception as e:
        print(f"[OCR ERROR] Something went wrong during OCR processing: {e}")
        
    return documents