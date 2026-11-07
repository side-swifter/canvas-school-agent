"""Local OCR for image-only PDFs and images (RapidOCR + PyMuPDF; optional extra `ocr`). Nothing leaves the machine."""
import logging

log = logging.getLogger('school-agent.ocr')
_engine = None
IMAGE_EXT = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff')


def available():
    try:
        import fitz  # noqa: F401
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def _lines(result, min_score=0.5):
    """RapidOCR boxes -> text in reading order: boxes join a line when their vertical centres are within half a text height."""
    boxes = []
    for box, text, score in result or []:
        if float(score) >= min_score and text.strip():
            ys = [p[1] for p in box]; xs = [p[0] for p in box]
            boxes.append({'y': sum(ys) / len(ys), 'h': max(ys) - min(ys) or 1, 'x': min(xs), 't': text.strip()})
    boxes.sort(key=lambda b: b['y'])
    lines = []
    for b in boxes:
        if lines and abs(b['y'] - lines[-1]['y']) <= 0.5 * max(b['h'], lines[-1]['h']):
            lines[-1]['parts'].append(b)
        else:
            lines.append({'y': b['y'], 'h': b['h'], 'parts': [b]})
    return '\n'.join(' '.join(p['t'] for p in sorted(l['parts'], key=lambda p: p['x'])) for l in lines)


def ocr_image(data, engine=None):
    result, _ = (engine or _get_engine())(data)
    return _lines(result)


def ocr_pdf(content, max_pages=25, dpi=170, engine=None):
    import fitz
    doc = fitz.open(stream=content, filetype='pdf')
    pages = []
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        png = page.get_pixmap(dpi=dpi).tobytes('png')
        pages.append(ocr_image(png, engine))
    return '\n\n'.join(pages)
