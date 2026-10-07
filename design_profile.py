"""Learn a document design from a reference PDF and turn it into a reusable design profile.

The profile (JSON) holds page size, colours, fonts, type sizes, line height and margins, plus
helpers to apply it: pptx_colors()/fonts for PowerPoint, apply_docx() for Word, css() for HTML/PDF.
Reading only: the PDF is parsed (pypdf) and rendered to small images (pypdfium2); nothing in it is executed.

CLI: python -X utf8 design_profile.py <reference.pdf> <out.design.json> [--md <out.md>]
"""
import colorsys
import hashlib
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA = 'saikuru-design-profile/1'
MAX_BYTES = 50 * 1024 * 1024
MAX_PAGES_TEXT = 30
MAX_PAGES_RENDER = 8
PT_TO_MM = 25.4 / 72
FONTS_DIR = Path('C:/Windows/Fonts')

# Original PDF font name (subset prefix removed, lowercased, no separators) -> output family on this PC.
FAMILY_RULES = [
    (r'yugoth|yugo|游ゴシック', 'Yu Gothic', ('YuGothM.ttc', 'YuGothR.ttc')),
    (r'yumin|游明朝', 'Yu Mincho', ('yumin.ttf', 'YuMincho.ttc')),
    (r'meiryo|メイリオ', 'Meiryo', ('meiryo.ttc',)),
    (r'msgothic|mspgothic|msuigothic|ｍｓゴシック', 'MS Gothic', ('msgothic.ttc',)),
    (r'msmincho|mspmincho', 'MS Mincho', ('msmincho.ttc',)),
    (r'notosansjp|notosanscjk|sourcehansans|hiraginosans|hiraginokaku|hirakaku', 'Yu Gothic', ('YuGothM.ttc',)),
    (r'notoserifjp|notoserifcjk|sourcehanserif|hiraginomincho|hiramin', 'Yu Mincho', ('yumin.ttf',)),
    (r'bizudpgothic|bizudgothic', 'BIZ UDPGothic', ('BIZ-UDGothicR.ttc',)),
    (r'bizudpmincho|bizudmincho', 'BIZ UDPMincho', ('BIZ-UDMinchoM.ttc',)),
    (r'arial|helvetica|liberationsans|roboto|inter', 'Arial', ('arial.ttf',)),
    (r'times|liberationserif|georgia', 'Times New Roman', ('times.ttf',)),
    (r'segoe', 'Segoe UI', ('segoeui.ttf',)),
    (r'calibri', 'Calibri', ('calibri.ttf',)),
    (r'mincho|serif|明朝', 'Yu Mincho', ('yumin.ttf',)),
    (r'gothic|sans|ゴシック', 'Yu Gothic', ('YuGothM.ttc',)),
]
FALLBACK = ('Meiryo', ('meiryo.ttc',))
SERIF = {'Yu Mincho', 'MS Mincho', 'BIZ UDPMincho', 'Times New Roman'}


# ---------- colour helpers ----------
def hex_of(rgb):
    return '#%02x%02x%02x' % tuple(max(0, min(255, int(round(v)))) for v in rgb)


def rgb_of(value):
    value = value.lstrip('#')
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def luminance(rgb):
    def channel(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def hls(rgb):
    return colorsys.rgb_to_hls(*(c / 255 for c in rgb))


def saturation(rgb):
    h, l, s = hls(rgb)
    return s if 0.08 < l < 0.92 else 0.0


def distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def mix(a, b, t):
    return tuple(x * (1 - t) + y * t for x, y in zip(a, b))


def hue_gap(a, b):
    d = abs(hls(a)[0] - hls(b)[0]) * 360
    return min(d, 360 - d)


def merge(counter, radius=28):
    """Merge near-identical colours (anti-aliasing, JPEG noise) keeping the most frequent representative."""
    merged = []
    for rgb, weight in counter.most_common():
        for item in merged:
            if distance(item[0], rgb) < radius:
                item[1] += weight
                break
        else:
            merged.append([rgb, weight])
    return merged


# ---------- extraction ----------
def _matrix(a, b):
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def _fill_color(op, args):
    try:
        values = [float(v) for v in args if isinstance(v, (int, float)) or hasattr(v, 'as_numeric')]
    except (TypeError, ValueError):
        return None
    if op == b'g' and len(values) == 1 or op in (b'sc', b'scn') and len(values) == 1:
        return (values[0] * 255,) * 3
    if op == b'rg' or op in (b'sc', b'scn') and len(values) == 3:
        return tuple(v * 255 for v in values[:3]) if len(values) >= 3 else None
    if op == b'k' or op in (b'sc', b'scn') and len(values) == 4:
        if len(values) < 4:
            return None
        c, m, y, k = values[:4]
        return tuple(255 * (1 - min(1, x + k)) for x in (c, m, y))
    return None


def _font_name(font_dict):
    try:
        name = str(font_dict.get('/BaseFont', '')) if font_dict else ''
    except Exception:
        name = ''
    name = name.lstrip('/')
    return re.sub(r'^[A-Z]{6}\+', '', name)


def family_for(original):
    key = re.sub(r'[\s_\-,]', '', original).lower()
    for pattern, family, files in FAMILY_RULES:
        if re.search(pattern, key):
            if any((FONTS_DIR / f).is_file() for f in files):
                return family, next(str(FONTS_DIR / f) for f in files if (FONTS_DIR / f).is_file())
            break
    return FALLBACK[0], str(FONTS_DIR / FALLBACK[1][0])


def text_runs(reader, pages):
    """(text, size_pt, x, y, rgb, font, page_index) for each text show operation."""
    runs = []
    for index in pages:
        page = reader.pages[index]
        state = {'fill': (0, 0, 0), 'stack': []}

        def before(op, args, cm, tm, state=state):
            if op == b'q':
                state['stack'].append(state['fill'])
            elif op == b'Q' and state['stack']:
                state['fill'] = state['stack'].pop()
            else:
                color = _fill_color(op, args)
                if color is not None:
                    state['fill'] = color

        def visit(text, cm, tm, font_dict, font_size, state=state, index=index):
            chars = len(re.sub(r'\s', '', text or ''))
            if not chars or not font_size:
                return
            m = _matrix(tm, cm)
            scale = math.sqrt(m[2] ** 2 + m[3] ** 2) or math.sqrt(m[0] ** 2 + m[1] ** 2)
            size = float(font_size) * scale
            if 3 <= size <= 200:
                runs.append((text, round(size * 2) / 2, m[4], m[5], state['fill'], _font_name(font_dict), index, chars))

        try:
            page.extract_text(visitor_operand_before=before, visitor_text=visit)
        except Exception:
            continue
    return runs


def render_palette(path, pages):
    """(overall colour counts, saturated colour counts). Saturated colours are counted on a posterized copy so
    small accent marks (rules, bullets, badges) are not lost to the median-cut quantizer."""
    import pypdfium2 as pdfium
    from PIL import Image
    counter = Counter()
    vivid = Counter()
    document = pdfium.PdfDocument(str(path))
    try:
        for index in pages:
            page = document[index]
            width, height = page.get_size()
            scale = min(1.0, 500 / max(width, height))
            image = page.render(scale=scale).to_pil().convert('RGB')
            page.close()
            for count, rgb in image.resize((max(1, image.width // 2), max(1, image.height // 2)), Image.NEAREST).getcolors(1 << 20) or []:
                if saturation(rgb) >= 0.3:
                    vivid[tuple((v // 8) * 8 + 4 for v in rgb)] += count * 4
            quantized = image.quantize(colors=24, method=Image.Quantize.MEDIANCUT)
            palette = quantized.getpalette()
            for count, slot in quantized.getcolors(256) or []:
                counter[tuple(palette[slot * 3:slot * 3 + 3])] += count
    finally:
        document.close()
    return counter, vivid


def learn(path, name=None):
    from pypdf import PdfReader
    path = Path(path)
    if path.stat().st_size>MAX_BYTES:
        raise ValueError('参考PDFは50MB以内です。')
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError('参考PDFは50MBまでです。')
    if not raw.startswith(b'%PDF-'):
        raise ValueError('PDFの形式を確認できません。')
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError('パスワード付きPDFは読み込めません。')
    total = len(reader.pages)
    if not total:
        raise ValueError('ページがありません。')
    notes = []
    box = reader.pages[0].mediabox
    width_pt, height_pt = float(box.width), float(box.height)

    runs = text_runs(reader, range(min(total, MAX_PAGES_TEXT)))
    render_pages = sorted({0, *range(0, total, max(1, total // MAX_PAGES_RENDER))})[:MAX_PAGES_RENDER]
    overall, vivid = render_palette(path, render_pages)
    pixels = merge(overall)
    pixel_total = sum(w for _, w in pixels) or 1

    # --- type sizes ---
    size_chars = Counter()
    for run in runs:
        size_chars[run[1]] += run[7]
    char_total = sum(size_chars.values())
    if char_total < 20:
        notes.append('文字をほとんど取り出せませんでした（画像だけのPDFの可能性）。書体・文字サイズは標準値です。')
    body = size_chars.most_common(1)[0][0] if size_chars else 10.5
    larger = sorted((s for s, c in size_chars.items() if s >= body * 1.15 and c >= 2), reverse=True)
    smaller = [s for s, c in size_chars.items() if s <= body * 0.9 and c >= char_total * 0.005]
    title = larger[0] if larger else round(body * 2)
    headings = larger[1:] if len(larger) > 1 else []
    sizes = {'title': title,
             'heading1': headings[0] if headings else round((title + body) / 2 * 2) / 2,
             'heading2': headings[1] if len(headings) > 1 else round(body * 1.25 * 2) / 2,
             'body': body,
             'caption': max(smaller) if smaller else round(body * 0.85 * 2) / 2}
    for key in ('heading1', 'heading2'):
        if sizes[key] <= sizes['body']:
            sizes[key] = round(sizes['body'] * (1.4 if key == 'heading1' else 1.2) * 2) / 2

    # --- fonts ---
    def top_font(predicate):
        counter = Counter()
        for run in runs:
            if run[5] and predicate(run[1]):
                counter[run[5]] += run[7]
        return counter.most_common(1)[0][0] if counter else ''
    body_original = top_font(lambda s: abs(s - body) < 0.6)
    heading_original = top_font(lambda s: s >= body * 1.15) or body_original
    body_family, body_file = family_for(body_original)
    heading_family, heading_file = family_for(heading_original)
    if not body_original:
        notes.append('本文の書体名を取得できませんでした。メイリオを使います。')
    heading_bold = bool(re.search(r'bold|heavy|black|semibold|demi|w[6-9]|-b$|ゴシックb', heading_original, re.I))

    # --- colours ---
    background = max(pixels, key=lambda p: p[1])[0] if pixels else (255, 255, 255)

    def text_color(predicate):
        counter = Counter()
        for run in runs:
            if predicate(run[1]):
                counter[tuple(int(round(c)) for c in run[4])] += run[7]
        merged = merge(counter, 18)
        return max(merged, key=lambda p: p[1])[0] if merged else None
    text = text_color(lambda s: abs(s - body) < 0.6) or (34, 34, 34)
    heading = text_color(lambda s: s >= body * 1.15) or text

    saturated = Counter()
    for rgb, weight in merge(vivid, 36):
        if distance(rgb, background) > 60 and weight / pixel_total >= 0.0005:
            saturated[rgb] += weight / pixel_total
    for run in runs:
        rgb = tuple(int(round(c)) for c in run[4])
        if saturation(rgb) >= 0.25:
            saturated[rgb] += run[7] / max(char_total, 1) * 0.5
    candidates = [rgb for rgb, _ in sorted(merge(Counter({k: v * 10000 for k, v in saturated.items()}), 36), key=lambda p: -p[1])]
    if saturation(heading) >= 0.25:
        candidates = [heading] + [c for c in candidates if distance(c, heading) > 36]
    primary = candidates[0] if candidates else mix(heading, (40, 90, 140), 0.6)
    if not candidates:
        notes.append('はっきりした色（アクセント）が見つかりませんでした。見出し色をもとに補っています。')
    secondary = next((c for c in candidates[1:] if hue_gap(c, primary) >= 25), None) or mix(primary, (255, 255, 255), 0.35)
    lights = [rgb for rgb, w in pixels if luminance(rgb) > 0.6 and distance(rgb, background) > 12 and w / pixel_total > 0.003]
    surface = lights[0] if lights else mix(primary, background, 0.9)
    mids = [rgb for rgb, w in pixels if 0.12 < luminance(rgb) < 0.5 and saturation(rgb) < 0.2 and w / pixel_total > 0.001]
    muted = mids[0] if mids else mix(text, background, 0.45)
    if contrast(text, background) < 4.5:
        notes.append('本文色と背景のコントラストが4.5:1未満です（元PDFのまま）。')
    if contrast(muted, background) < 4.5:
        muted = mix(muted, text, 0.5)

    # --- margins and line height (approximate, from text positions) ---
    xs = [r[2] for r in runs]
    ys = [r[3] for r in runs]
    margins = None
    if len(xs) >= 10:
        def pct(values, q):
            values = sorted(values)
            return values[min(len(values) - 1, max(0, int(q * (len(values) - 1))))]
        rights = [r[2] + r[7] * r[1] * 0.55 for r in runs if abs(r[1] - body) < 0.6]
        margins = {'left': round(max(0, pct(xs, 0.03)) * PT_TO_MM, 1),
                   'right': round(max(0, width_pt - pct(rights or xs, 0.97)) * PT_TO_MM, 1),
                   'top': round(max(0, height_pt - pct(ys, 0.98) - title) * PT_TO_MM, 1),
                   'bottom': round(max(0, pct(ys, 0.02) - body * 0.3) * PT_TO_MM, 1)}
    else:
        notes.append('余白を推定できませんでした。標準値（20mm）を使います。')
    default_margin = 20.0
    margins = {k: (v if v is not None and 5 <= v <= 60 else default_margin) for k, v in (margins or {}).items()} or \
        {k: default_margin for k in ('top', 'bottom', 'left', 'right')}
    gaps = []
    by_page = defaultdict(list)
    for r in runs:
        if abs(r[1] - body) < 0.6:
            by_page[r[6]].append(r[3])
    for values in by_page.values():
        lines = sorted({round(v, 1) for v in values}, reverse=True)
        gaps += [a - b for a, b in zip(lines, lines[1:]) if body * 0.9 < a - b < body * 3]
    line_height = round(statistics.median(gaps) / body, 2) if gaps else 1.6

    profile = {
        'schema': SCHEMA,
        'name': name or path.stem,
        'source': {'file': path.name, 'sha256': hashlib.sha256(raw).hexdigest(), 'pages': total,
                   'text_pages_read': min(total, MAX_PAGES_TEXT), 'pages_rendered': [p + 1 for p in render_pages]},
        'page': {'width_mm': round(width_pt * PT_TO_MM, 1), 'height_mm': round(height_pt * PT_TO_MM, 1),
                 'orientation': 'landscape' if width_pt > height_pt else 'portrait'},
        'colors': {'background': hex_of(background), 'surface': hex_of(surface), 'text': hex_of(text),
                   'heading': hex_of(heading), 'primary': hex_of(primary), 'secondary': hex_of(secondary),
                   'muted': hex_of(muted),
                   'palette': [{'hex': hex_of(rgb), 'share': round(w / pixel_total, 4)} for rgb, w in
                               sorted(pixels, key=lambda p: -p[1])[:10]]},
        'fonts': {'heading': {'family': heading_family, 'original': heading_original, 'bold': heading_bold,
                              'file': heading_file, 'serif': heading_family in SERIF},
                  'body': {'family': body_family, 'original': body_original, 'file': body_file,
                           'serif': body_family in SERIF},
                  'detected': [n for n, _ in Counter(r[5] for r in runs if r[5]).most_common(8)]},
        'sizes_pt': sizes,
        'line_height': max(1.1, min(2.2, line_height)),
        'margins_mm': margins,
        'notes': notes,
    }
    validate(profile)
    return profile


# ---------- validation and application ----------
HEX = re.compile(r'^#[0-9a-fA-F]{6}$')


def validate(profile):
    """Strict shape check before a profile is used to render anything (profiles are data, never code)."""
    if not isinstance(profile, dict) or profile.get('schema') != SCHEMA:
        raise ValueError('デザインプロファイルの形式が違います（schema）。')
    colors = profile.get('colors') or {}
    for key in ('background', 'surface', 'text', 'heading', 'primary', 'secondary', 'muted'):
        if not HEX.match(str(colors.get(key, ''))):
            raise ValueError('デザインプロファイルの色が不正です: ' + key)
    for role in ('heading', 'body'):
        family = str((profile.get('fonts') or {}).get(role, {}).get('family', ''))
        if not re.fullmatch(r'[A-Za-z0-9 ]{1,40}', family):
            raise ValueError('デザインプロファイルの書体名が不正です: ' + role)
    for key, value in (profile.get('sizes_pt') or {}).items():
        if not isinstance(value, (int, float)) or not 4 <= value <= 200:
            raise ValueError('デザインプロファイルの文字サイズが不正です: ' + key)
    for key, value in (profile.get('margins_mm') or {}).items():
        if not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise ValueError('デザインプロファイルの余白が不正です: ' + key)
    if not isinstance(profile.get('line_height'), (int, float)) or not 1 <= profile['line_height'] <= 3:
        raise ValueError('デザインプロファイルの行間が不正です。')
    return profile


def load(path):
    return validate(json.loads(Path(path).read_text(encoding='utf-8')))


def font_file(profile, role):
    """Installed font file for raster output (PDF/MP4 frames); falls back to Meiryo."""
    family = profile['fonts'][role]['family']
    for pattern, name, files in FAMILY_RULES + [('', FALLBACK[0], FALLBACK[1])]:
        if name == family:
            for f in files:
                if (FONTS_DIR / f).is_file():
                    return str(FONTS_DIR / f)
    return str(FONTS_DIR / 'meiryo.ttc')


def css(profile):
    """CSS for HTML documents (and print to PDF): variables plus base element rules."""
    c, f, s, m = profile['colors'], profile['fonts'], profile['sizes_pt'], profile['margins_mm']
    generic = lambda role: 'serif' if f[role].get('serif') else 'sans-serif'
    return (
        ':root{'
        f"--color-bg:{c['background']};--color-surface:{c['surface']};--color-text:{c['text']};"
        f"--color-heading:{c['heading']};--color-primary:{c['primary']};--color-secondary:{c['secondary']};"
        f"--color-muted:{c['muted']};--font-heading:'{f['heading']['family']}',{generic('heading')};"
        f"--font-body:'{f['body']['family']}',{generic('body')};--size-body:{s['body']}pt;"
        f"--line-height:{profile['line_height']}}}\n"
        f"@page{{size:{profile['page']['width_mm']}mm {profile['page']['height_mm']}mm;"
        f"margin:{m['top']}mm {m['right']}mm {m['bottom']}mm {m['left']}mm}}\n"
        'body{margin:0;background:var(--color-bg);color:var(--color-text);font-family:var(--font-body);'
        'font-size:var(--size-body);line-height:var(--line-height)}\n'
        f"h1{{font-family:var(--font-heading);color:var(--color-heading);font-size:{s['title']}pt;line-height:1.25"
        f"{';font-weight:700' if f['heading'].get('bold') else ''}}}\n"
        f"h2{{font-family:var(--font-heading);color:var(--color-heading);font-size:{s['heading1']}pt;line-height:1.3;"
        'border-bottom:2px solid var(--color-primary);padding-bottom:.2em}\n'
        f"h3{{font-family:var(--font-heading);color:var(--color-primary);font-size:{s['heading2']}pt;line-height:1.35}}\n"
        f"small,figcaption,.caption{{color:var(--color-muted);font-size:{s['caption']}pt}}\n"
        'a{color:var(--color-primary)}\n'
        'table{border-collapse:collapse}th{background:var(--color-primary);color:var(--color-bg);text-align:left}'
        'th,td{border:1px solid var(--color-surface);padding:.3em .6em}tr:nth-child(even) td{background:var(--color-surface)}\n'
    )


def apply_docx(doc, profile, page_setup=True):
    """Apply the profile to a python-docx Document: base and heading styles, colours, page size and margins."""
    from docx.shared import Pt, Mm, RGBColor
    from docx.oxml.ns import qn
    c, f, s, m = profile['colors'], profile['fonts'], profile['sizes_pt'], profile['margins_mm']

    def style_font(style, family, size, color, bold=None):
        font = style.font
        font.name = family
        font.size = Pt(size)
        font.color.rgb = RGBColor(*rgb_of(color))
        if bold is not None:
            font.bold = bold
        rpr = style.element.get_or_add_rPr()
        fonts = rpr.find(qn('w:rFonts'))
        if fonts is None:
            fonts = rpr.makeelement(qn('w:rFonts'), {})
            rpr.append(fonts)
        for attr in ('w:ascii', 'w:hAnsi', 'w:eastAsia', 'w:cs'):
            fonts.set(qn(attr), family)
        for attr in ('w:asciiTheme', 'w:hAnsiTheme', 'w:eastAsiaTheme', 'w:cstheme'):
            fonts.attrib.pop(qn(attr), None)

    styles = doc.styles
    style_font(styles['Normal'], f['body']['family'], s['body'], c['text'])
    styles['Normal'].paragraph_format.line_spacing = profile['line_height']
    bold = bool(f['heading'].get('bold', True))
    for name, size, color in (('Title', s['title'], c['heading']), ('Heading 1', s['heading1'], c['heading']),
                              ('Heading 2', s['heading2'], c['primary']), ('Heading 3', s['body'] * 1.1, c['primary'])):
        try:
            style_font(styles[name], f['heading']['family'], size, color, bold)
        except KeyError:
            continue
    try:
        style_font(styles['Caption'], f['body']['family'], s['caption'], c['muted'])
    except KeyError:
        pass
    if page_setup:
        for section in doc.sections:
            if profile['page']['width_mm'] and profile['page']['height_mm']:
                section.page_width, section.page_height = Mm(profile['page']['width_mm']), Mm(profile['page']['height_mm'])
            section.top_margin, section.bottom_margin = Mm(m['top']), Mm(m['bottom'])
            section.left_margin, section.right_margin = Mm(m['left']), Mm(m['right'])
    return doc


def style_docx_table(table, profile):
    """Header row in the primary colour (bold, readable text), even rows on the surface colour, light borders."""
    from docx.shared import RGBColor
    from docx.oxml.ns import qn
    c = profile['colors']
    on_primary = 'ffffff' if contrast(rgb_of(c['primary']), (255, 255, 255)) >= 4.5 else '1a1a1a'

    def shade(cell, fill):
        tcpr = cell._tc.get_or_add_tcPr()
        for old in tcpr.findall(qn('w:shd')):
            tcpr.remove(old)
        shd = tcpr.makeelement(qn('w:shd'), {qn('w:val'): 'clear', qn('w:color'): 'auto', qn('w:fill'): fill})
        tcpr.append(shd)

    tblpr = table._tbl.tblPr
    borders = tblpr.find(qn('w:tblBorders'))
    if borders is None:
        borders = tblpr.makeelement(qn('w:tblBorders'), {})
        tblpr.append(borders)
    for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        element = borders.find(qn('w:' + edge))
        if element is None:
            element = borders.makeelement(qn('w:' + edge), {})
            borders.append(element)
        element.set(qn('w:val'), 'single')
        element.set(qn('w:sz'), '4')
        element.set(qn('w:color'), c['surface'].lstrip('#') if contrast(rgb_of(c['surface']), rgb_of(c['background'])) > 1.2 else c['muted'].lstrip('#'))
    for r, row in enumerate(table.rows):
        for cell in row.cells:
            if r == 0:
                shade(cell, c['primary'].lstrip('#'))
            elif r % 2 == 0:
                shade(cell, c['surface'].lstrip('#'))
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    if r == 0:
                        run.font.bold = True
                        run.font.color.rgb = RGBColor.from_string(on_primary.upper())
    return table


def slide_palette(profile):
    """Colour roles for slide/PPTX rendering (all '#rrggbb')."""
    c = profile['colors']
    dark_bg = luminance(rgb_of(c['background'])) < 0.2
    palette = {'accent': c['primary'], 'dark': c['heading'], 'text': c['text'], 'bg': c['background'],
               'light': c['surface'], 'muted': c['muted'], 'on_accent': '#ffffff' if contrast(rgb_of(c['primary']), (255, 255, 255)) >= 3 else '#1a1a1a',
               'dark_background': dark_bg, 'heading_font': profile['fonts']['heading']['family'],
               'body_font': profile['fonts']['body']['family']}
    # Spot colour only when the PDF really had a second distinct colour; a tint derived from the primary
    # (same hue) is left out so nothing appears that the reference did not use.
    second = rgb_of(c['secondary'])
    if saturation(second) >= 0.25 and hue_gap(second, rgb_of(c['primary'])) >= 25:
        palette['secondary'] = c['secondary']
    return palette


def summary_markdown(profile):
    c, f, s, m = profile['colors'], profile['fonts'], profile['sizes_pt'], profile['margins_mm']
    lines = [f"# デザインプロファイル：{profile['name']}", '',
             f"- 元PDF：{profile['source']['file']}（{profile['source']['pages']}ページ、SHA256 {profile['source']['sha256'][:12]}…）",
             f"- 用紙：{profile['page']['width_mm']}×{profile['page']['height_mm']}mm（{'横' if profile['page']['orientation'] == 'landscape' else '縦'}）",
             '', '## 色', '| 役割 | 色 |', '|---|---|']
    labels = {'background': '背景', 'surface': '面（表・枠の地）', 'text': '本文', 'heading': '見出し',
              'primary': 'メイン（アクセント）', 'secondary': 'サブ', 'muted': '補足文字'}
    lines += [f'| {labels[k]} | `{c[k]}` |' for k in labels]
    lines += ['', '## 書体', f"- 見出し：{f['heading']['family']}（元：{f['heading']['original'] or '不明'}{'、太字' if f['heading'].get('bold') else ''}）",
              f"- 本文：{f['body']['family']}（元：{f['body']['original'] or '不明'}）",
              '', '## 文字サイズ（pt）',
              f"タイトル {s['title']}／見出し1 {s['heading1']}／見出し2 {s['heading2']}／本文 {s['body']}／注記 {s['caption']}",
              f"- 行間：本文サイズの {profile['line_height']} 倍",
              f"- 余白（mm、推定）：上 {m['top']}／下 {m['bottom']}／左 {m['left']}／右 {m['right']}"]
    if profile['notes']:
        lines += ['', '## 注意'] + ['- ' + n for n in profile['notes']]
    lines += ['', '書体は元PDFの書体に近い、このPCにある書体に置き換えています。余白と行間は文字の位置からの推定値です。']
    return '\n'.join(lines) + '\n'


def main(argv):
    if len(argv) < 2:
        raise SystemExit('usage: design_profile.py <reference.pdf> <out.design.json> [--md out.md] [--name NAME]')
    name = argv[argv.index('--name') + 1] if '--name' in argv else None
    profile = learn(argv[0], name)
    Path(argv[1]).write_bytes(json.dumps(profile, ensure_ascii=False, indent=2).encode('utf-8'))
    if '--md' in argv:
        Path(argv[argv.index('--md') + 1]).write_bytes(summary_markdown(profile).encode('utf-8'))
    print(json.dumps({'ok': True, 'colors': profile['colors'], 'fonts': {k: v['family'] for k, v in profile['fonts'].items() if k != 'detected'},
                      'sizes_pt': profile['sizes_pt'], 'notes': profile['notes']}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except ValueError as exc:
        sys.stderr.write('SAIKURU_INPUT_ERROR: ' + str(exc)[:500].replace('\n', ' ') + '\n')
        sys.exit(2)
