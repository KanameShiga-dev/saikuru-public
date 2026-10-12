"""Deterministic local rendering of reference text into PNG pages (参考コンテキストの画像化).

No AI, no network, no SVG/HTML: the text is drawn as plain characters with a fixed font. The text is never
shortened, summarised or shrunk to fit; if it does not fit in the page limit the caller falls back to text.
"""
import io
import math
import unicodedata
from pathlib import Path

RENDERER_VERSION = 'pillow-text-2'
FONT_CANDIDATES = (Path(r'C:\Windows\Fonts\YuGothM.ttc'), Path(r'C:\Windows\Fonts\meiryo.ttc'),
                   Path(r'C:\Windows\Fonts\msgothic.ttc'))
MARGIN = 32
LINE_RATIO = 1.5
MAX_PNG_BYTES = 2 * 1024 * 1024


class RenderError(ValueError):
    """The text cannot be rendered faithfully (the caller sends text instead)."""


def _font_path():
    for path in FONT_CANDIDATES:
        if path.is_file():
            return path
    raise RenderError('font_missing')


def unsupported_characters(text):
    """Characters the fixed font may not draw (emoji and other non-BMP, private use, control characters)."""
    return sorted({c for c in text if ord(c) > 0xFFFF or unicodedata.category(c) in ('Co', 'Cs', 'Cn')
                   or (unicodedata.category(c) == 'Cc' and c not in '\n\t')})


_GLYPHS = {}   # (font path, size, character) -> drawn by the font; the answer never changes for a font file


def _missing_glyphs(font, text):
    """Characters the font draws as its "no glyph" box (compared with a private-use code point)."""
    notdef = None
    missing = []
    for char in set(text) - set(' \n\t\u3000'):
        key = (font.path, font.size, char)
        if key not in _GLYPHS:
            if notdef is None:
                notdef = font.getmask('\ue000')
                blank = bytes(notdef)
            mask = font.getmask(char)
            _GLYPHS[key] = not (mask.size == notdef.size and bytes(mask) == blank)
        if not _GLYPHS[key]:
            missing.append(char)
    return sorted(missing)


def layout(text, width, height, font_px):
    """Wrap the text into lines by measured glyph width. Returns (font, lines, lines_per_page)."""
    try:
        from PIL import ImageFont
    except ImportError as exc:
        raise RenderError('pillow_missing') from exc
    if unsupported_characters(text):
        raise RenderError('unsupported_characters')
    try:
        font = ImageFont.truetype(str(_font_path()), font_px)
    except OSError as exc:
        raise RenderError('font_unavailable') from exc
    if _missing_glyphs(font, text):
        raise RenderError('missing_glyph')
    usable = width - 2 * MARGIN
    lines = []
    advance = {}
    for paragraph in text.replace('\r\n', '\n').replace('\r', '\n').replace('\t', '    ').split('\n'):
        line, length = '', 0.0
        for char in paragraph:
            if char not in advance:
                advance[char] = font.getlength(char)
            if line and length + advance[char] > usable:
                lines.append(line)
                line, length = '', 0.0
            line += char
            length += advance[char]
        lines.append(line)
    # The sum of single advances can differ slightly from the drawn width (kerning): check each line as drawn and
    # move characters to the next line rather than letting any text run past the margin.
    checked = []
    for line in lines:
        while line and font.getlength(line) > usable:
            cut = len(line) - 1
            while cut > 1 and font.getlength(line[:cut]) > usable:
                cut -= 1
            checked.append(line[:cut])
            line = line[cut:]
        checked.append(line)
    lines = checked
    line_height = math.ceil(font_px * LINE_RATIO)
    per_page = (height - 2 * MARGIN) // line_height
    if per_page < 1:
        raise RenderError('page_too_small')
    return font, lines, per_page


def _height(rows, font_px, height):
    # The last page is cut to its text so that no blank area is sent (an image costs by its area).
    return min(height, 2 * MARGIN + rows * math.ceil(font_px * LINE_RATIO))


def page_sizes(text, width, height, font_px):
    """[(width, height), ...] of the pages the text needs, without drawing."""
    _, lines, per_page = layout(text, width, height, font_px)
    return [(width, _height(len(lines[start:start + per_page]), font_px, height))
            for start in range(0, len(lines), per_page)]


def page_count(text, width, height, font_px):
    return len(page_sizes(text, width, height, font_px))


def render(text, width, height, font_px, max_pages):
    """Return a list of PNG bytes. Raises RenderError when the text needs more than max_pages pages."""
    from PIL import Image, ImageDraw
    font, lines, per_page = layout(text, width, height, font_px)
    if math.ceil(len(lines) / per_page) > max_pages:
        raise RenderError('too_many_pages')
    line_height = math.ceil(font_px * LINE_RATIO)
    result = []
    for start in range(0, len(lines), per_page):
        rows = lines[start:start + per_page]
        image = Image.new('L', (width, _height(len(rows), font_px, height)), 255)
        draw = ImageDraw.Draw(image)
        for row, line in enumerate(rows):
            draw.text((MARGIN, MARGIN + row * line_height), line, font=font, fill=0)
        buffer = io.BytesIO()
        image.save(buffer, format='PNG', optimize=False)   # no text chunks: nothing but the pixels is stored
        data = buffer.getvalue()
        if len(data) > MAX_PNG_BYTES:
            raise RenderError('image_too_large')
        result.append(data)
    return result
