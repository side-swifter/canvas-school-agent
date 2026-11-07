"""Extract course document text without executing embedded content."""
from io import BytesIO
from html import unescape
from html.parser import HTMLParser
from docx import Document
from pypdf import PdfReader

class Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)
    def handle_starttag(self, tag, attrs):
        if tag in ('p', 'br', 'li', 'tr', 'td', 'div', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

def strip_html(raw):
    parser = Stripper()
    parser.feed(raw or '')
    return '\n'.join(' '.join(part.split()) for part in unescape(' '.join(parser.parts)).split('\n') if part.strip()).replace('  ', ' ')

def extract_document(content, filename):
    name = (filename or '').lower().split('?')[0]
    if name.endswith('.pdf'):
        reader = PdfReader(BytesIO(content))
        return '\n'.join(page.extract_text() or '' for page in reader.pages[:100])
    if name.endswith('.docx'):
        doc = Document(BytesIO(content))
        segments = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                segments.append(' | '.join(cell.text for cell in row.cells))
        return '\n'.join(segments)
    if name.endswith(('.txt', '.csv', '.md', '.html', '.htm')):
        value = content.decode('utf-8', errors='replace')
        return strip_html(value) if name.endswith(('.html', '.htm')) else value
    return ''
