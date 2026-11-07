"""Extract course document text without executing embedded content."""
import re
from io import BytesIO
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin
from docx import Document
from pypdf import PdfReader

BLOCK_TAGS = ('p', 'br', 'li', 'tr', 'div', 'h1', 'h2', 'h3', 'h4', 'table')


class Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        if tag in BLOCK_TAGS:
            self.parts.append('\n')
        if tag in ('td', 'th'):
            self.parts.append(' | ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.skip:
            self.skip -= 1


class LinkFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'a' and a.get('href'):
            self.links.append(a['href'])
        if tag in ('iframe', 'embed') and a.get('src'):
            self.links.append(a['src'])


def strip_html(raw):
    parser = Stripper()
    parser.feed(raw or '')
    text = unescape(''.join(parser.parts))
    return '\n'.join(' '.join(line.split()) for line in text.split('\n') if line.strip())


def extract_links(raw_html, base_url):
    finder = LinkFinder()
    finder.feed(raw_html or '')
    seen, out = set(), []
    for href in finder.links:
        absolute = urljoin(base_url + '/', unescape(href))
        if absolute.startswith(('http://', 'https://')) and absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


class NeedsOCR(Exception):
    """PDF has pages but no extractable text (scanned). OCR is deliberately not run automatically."""


def extract_document(content, filename):
    name = (filename or '').lower().split('?')[0]
    if name.endswith('.pdf'):
        reader = PdfReader(BytesIO(content))
        text = '\n'.join(page.extract_text() or '' for page in reader.pages[:100])
        if len(reader.pages) and len(text.strip()) < 20:
            raise NeedsOCR(filename)
        return text
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
    if name.endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff')):
        raise NeedsOCR(filename)
    return ''


SUPPORTED_EXT = ('.pdf', '.docx', '.txt', '.csv', '.md', '.html', '.htm', '.png', '.jpg', '.jpeg')
GOOGLE_DOC_RE = re.compile(r'https://docs\.google\.com/(document|spreadsheets|presentation)/d/(e/)?([\w-]{15,})')


def google_export_url(url):
    """(fetch_url, doc_id, filename) for a Google Doc/Sheet/Slides link, or None.
    /d/e/ links are 'published to web' pages (HTML); normal links use the export endpoint (needs link-sharing or a logged-in session)."""
    m = GOOGLE_DOC_RE.match(url)
    if not m:
        return None
    kind, published, doc_id = m.groups()
    if published:
        return f'https://docs.google.com/{kind}/d/e/{doc_id}/pub', doc_id, 'linked.html'
    fmt = {'document': 'txt', 'spreadsheets': 'csv', 'presentation': 'txt'}[kind]
    return f'https://docs.google.com/{kind}/d/{doc_id}/export?format={fmt}', doc_id, 'linked.txt'
