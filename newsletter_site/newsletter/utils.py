# newsletter/utils.py
import base64
import hashlib
import io
import logging
import re
from datetime import datetime

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from googleapiclient.discovery import build
from google.oauth2 import service_account
from PIL import Image, ImageOps

from .models import BODY_IMAGE_MAX_PX, BODY_IMAGE_QUALITY, FULL_IMAGE_QUALITY, _compress_image_bytes

logger = logging.getLogger(__name__)

# Google Docs' HTML export embeds pasted-in images as base64 data URIs
# directly in the markup. Matches an <img ...> tag with such a src and
# captures the surrounding attributes so they can be preserved on rewrite.
INLINE_IMG_REGEX = re.compile(
    r'<img([^>]*?)src="data:image/(?:png|jpe?g|gif);base64,(?P<data>[^"]+)"([^>]*?)>',
    re.IGNORECASE
)

# Basic regex for top lines like:
# Title: ...
# Writer(s): ...
# Date: ...
# Issue: ...
# Type of Article: ...
TITLE_REGEX = re.compile(r"^Title:\s*(.+)$", re.IGNORECASE)
WRITER_REGEX = re.compile(r"^Writer\(s\):\s*(.+)$", re.IGNORECASE)
DATE_REGEX = re.compile(r"^Date:\s*(.+)$", re.IGNORECASE)
ISSUE_REGEX = re.compile(r"^Issue:\s*(\d+)$", re.IGNORECASE)
TYPE_REGEX = re.compile(r"^Type of Article:\s*(.+)$", re.IGNORECASE)

# Google's export puts all text in non-nested <span style="..."> runs.
SPAN_REGEX = re.compile(r'<span\s+style="([^"]*)">(.*?)</span>', re.IGNORECASE | re.DOTALL)
FONT_SIZE_REGEX = re.compile(r'font-size:\s*([\d.]+)pt', re.IGNORECASE)


def _body_font_size(html):
    """Most common span font size (in pt), weighted by amount of text."""
    weights = {}
    for style, inner in SPAN_REGEX.findall(html):
        size = FONT_SIZE_REGEX.search(style)
        text_len = len(re.sub(r'<[^>]+>|&nbsp;', '', inner).strip())
        if size and text_len:
            weights[size.group(1)] = weights.get(size.group(1), 0) + text_len
    return float(max(weights, key=weights.get)) if weights else None


def _relative_size(size_match, base_size):
    """'font-size:1.25em' for a pt size relative to the body size, or None
    when it matches the body size (or either is unknown)."""
    if not size_match or not base_size:
        return None
    ratio = round(float(size_match.group(1)) / base_size, 2)
    return None if ratio == 1 else f'font-size:{ratio:g}em'

def strip_gdoc_html(raw_html: str) -> str:
    """
    Cleans up Google Docs HTML export to make it readable and styled with site CSS.
    - Removes inline styles and CSS
    - Converts footnote links to proper superscripts
    - Preserves semantic HTML (em, strong, links)
    - Removes Google's list styling junk
    """
    # Remove DOCTYPE if present
    raw_html = re.sub(r'<!DOCTYPE.*?>', '', raw_html, flags=re.IGNORECASE)

    # Remove entire <style> blocks (Google Docs injects tons of CSS)
    raw_html = re.sub(r'<style[^>]*>.*?</style>', '', raw_html, flags=re.IGNORECASE|re.DOTALL)

    # Remove entire <html> and <head> tags
    raw_html = re.sub(r'</?(html|head)\b[^>]*>', '', raw_html, flags=re.IGNORECASE)

    # Remove <meta ...> tags
    raw_html = re.sub(r'<meta[^>]*>', '', raw_html, flags=re.IGNORECASE)

    # Extract everything inside the <body>...</body>, removing the body tags themselves
    raw_html = re.sub(r'<body\b[^>]*>(.*?)</body>', r'\1', raw_html, flags=re.IGNORECASE|re.DOTALL)

    # Body text size = the most common span font size, weighted by text length.
    # Other sizes are kept relative to it, so the doc's main text renders at
    # the site's normal size whatever point size the doc happens to use.
    base_size = _body_font_size(raw_html)

    # Wrap italic/bold span contents in <em>/<strong>, keeping the span itself
    # so its font size survives the style cleanup below.
    def mark_emphasis(match):
        style, inner = match.group(1), match.group(2)
        if re.search(r'font-style:\s*italic', style, re.IGNORECASE):
            inner = f'<em>{inner}</em>'
        if re.search(r'font-weight:\s*(?:700|bold)', style, re.IGNORECASE):
            inner = f'<strong>{inner}</strong>'
        return f'<span style="{style}">{inner}</span>'

    raw_html = SPAN_REGEX.sub(mark_emphasis, raw_html)

    # Blank lines in the doc are empty paragraphs. Keep them as &nbsp; lines
    # sized like the doc's blank line, instead of letting them be stripped as
    # empty tags.
    def keep_blank_line(match):
        inner = match.group(1)
        # Image-only paragraphs have no text but aren't blank.
        if re.sub(r'<[^>]+>', '', inner).strip() or re.search(r'<img\b', inner, re.IGNORECASE):
            return match.group(0)
        size = FONT_SIZE_REGEX.search(inner)
        return f'<p style="{_relative_size(size, base_size) or ""}">&nbsp;</p>'

    raw_html = re.sub(r'<p\b[^>]*>(.*?)</p>', keep_blank_line, raw_html, flags=re.IGNORECASE | re.DOTALL)

    # Strip inline styles down to the formatting worth keeping: alignment and
    # indentation on block elements, relative font size on spans.
    def keep_layout_styles(match):
        tag, attrs, style = match.group(1), match.group(2), match.group(3)
        kept = []
        if tag.lower() == 'span':
            size = _relative_size(FONT_SIZE_REGEX.search(style), base_size)
            if size:
                kept.append(size)
        else:
            for prop in ('text-align', 'margin-left', 'text-indent', 'font-size'):
                m = re.search(rf'(?<![-\w]){prop}:\s*([^;]+)', style, re.IGNORECASE)
                if not m or m.group(1).strip() in ('0', '0pt'):
                    continue
                # Paragraph-level font-size is Google's default, not what's
                # displayed; only keep the em sizes set for blank lines above.
                if prop == 'font-size' and not m.group(1).strip().endswith('em'):
                    continue
                kept.append(f'{prop}:{m.group(1).strip()}')
        if not kept:
            return f'<{tag}{attrs}'
        return f'<{tag}{attrs} style="{"; ".join(kept)}"'

    raw_html = re.sub(r'<(\w+)\b([^>]*?)\s+style="([^"]*)"', keep_layout_styles, raw_html, flags=re.IGNORECASE)

    # Spans left with no styling are just noise.
    raw_html = re.sub(r'<span>(.*?)</span>', r'\1', raw_html, flags=re.IGNORECASE | re.DOTALL)

    # Remove all class attributes (Google adds lots of junk classes)
    raw_html = re.sub(r'\s+class="[^"]*"', '', raw_html, flags=re.IGNORECASE)

    # Fix footnote references FIRST (before removing IDs): Convert <sup><a>[1]</a></sup> to proper format
    # Keep the href and convert [1] to 1
    raw_html = re.sub(r'<sup[^>]*><a\s+href="([^"]+)"[^>]*>\[(\d+)\]</a></sup>', r'<sup><a href="\1">\2</a></sup>', raw_html)

    # Also fix standalone [1] in superscripts
    raw_html = re.sub(r'<sup[^>]*>\[(\d+)\]</sup>', r'<sup>\1</sup>', raw_html)

    # Fix footnote markers at bottom: Keep the id attribute but remove brackets
    # <a href="#ftnt_ref1" id="ftnt1">[1]</a> -> <a href="#ftnt_ref1" id="ftnt1">1</a>
    raw_html = re.sub(r'(<a[^>]*>)\[(\d+)\](</a>)', r'\g<1>\2\g<3>', raw_html)

    # NOW remove id attributes EXCEPT for footnote anchors (ftnt and ftnt_ref)
    # Remove id attributes that are NOT footnote-related
    raw_html = re.sub(r'\s+id="(?!ftnt)[^"]*"', '', raw_html, flags=re.IGNORECASE)

    # Google Docs often exports italics in the footnotes section within a <div>
    # Let's wrap the footnotes section to preserve formatting
    # Match the footnotes section (starts with <hr> and contains footnote references)
    footnotes_match = re.search(r'(<hr>.*)', raw_html, flags=re.DOTALL)
    if footnotes_match:
        footnotes_html = footnotes_match.group(1)
        # Wrap footnotes in a special div so we can style them
        raw_html = raw_html[:footnotes_match.start()] + '<div class="footnotes">' + footnotes_html + '</div>'

    # Remove empty tags that might be left over
    raw_html = re.sub(r'<(\w+)[^>]*>\s*</\1>', '', raw_html)

    # Clean up multiple consecutive spaces/newlines
    raw_html = re.sub(r'\n\s*\n\s*\n+', '\n\n', raw_html)

    # Remove trailing/leading whitespace from paragraphs
    raw_html = re.sub(r'<p[^>]*>\s+', '<p>', raw_html)
    raw_html = re.sub(r'\s+</p>', '</p>', raw_html)

    # Remove horizontal rules that are just styling artifacts
    raw_html = re.sub(r'<hr[^>]*>', '<hr>', raw_html)

    # Marks content cleaned by this version, whose spacing comes from the
    # doc's own blank lines (see .gdoc in base.html) rather than paragraph margins.
    return f'<div class="gdoc">{raw_html.strip()}</div>'


UPLOAD_DIR = "article_body_images/uploads"


def _full_resolution_bytes(raw):
    """Every pixel of the original, re-saved as a quality-95 JPEG (camera
    files are often saved at wastefully high quality). Keeps the original
    bytes if that's not smaller, or for non-JPEGs (PNG transparency etc.)."""
    img = Image.open(io.BytesIO(raw))
    if img.format != "JPEG":
        return raw, ".png" if img.format == "PNG" else ".jpg"
    img = ImageOps.exif_transpose(img).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=FULL_IMAGE_QUALITY, optimize=True)
    resaved = buf.getvalue()
    return (resaved if len(resaved) < len(raw) else raw), ".jpg"


def save_uploaded_body_image(raw):
    """Store an image inserted in the article editor. Saves a display copy
    (BODY_IMAGE_MAX_PX, for the article page) and, when that copy lost
    detail, a full-resolution copy at <name>_full.<ext> for readers to click
    through to. Returns the display copy's URL, or None if raw isn't an image.
    Named by content hash, so re-uploading the same photo reuses its files."""
    display, ext = _compress_image_bytes(
        raw, max_px=BODY_IMAGE_MAX_PX, quality=BODY_IMAGE_QUALITY, keep_under_bytes=1_500_000,
    )
    if display is None:
        return None
    name = f"{UPLOAD_DIR}/{hashlib.sha1(raw).hexdigest()[:16]}"
    path = f"{name}{ext}"
    if not default_storage.exists(path):
        default_storage.save(path, ContentFile(display))
    if display is not raw:
        full, full_ext = _full_resolution_bytes(raw)
        full_path = f"{name}_full{full_ext}"
        if not default_storage.exists(full_path):
            default_storage.save(full_path, ContentFile(full))
    return default_storage.url(path)


# An <img>, optionally already wrapped in a link (group 1).
LINKABLE_IMG_REGEX = re.compile(r'(<a\b[^>]*>\s*)?(<img\b[^>]*\bsrc="([^"]+)"[^>]*>)', re.IGNORECASE)


def link_images_to_full_size(html):
    """Wrap each uploaded article image that has a full-resolution copy in a
    link to it, so readers can click through to every pixel."""
    media_prefix = f"{settings.MEDIA_URL}{UPLOAD_DIR}/"

    def _wrap(match):
        already_linked, img_tag, src = match.group(1), match.group(2), match.group(3)
        if already_linked or not src.startswith(media_prefix):
            return match.group(0)
        stem = src[len(settings.MEDIA_URL):].rsplit(".", 1)[0]
        for ext in (".jpg", ".png"):
            full_path = f"{stem}_full{ext}"
            if default_storage.exists(full_path):
                url = default_storage.url(full_path)
                return f'<a href="{url}" target="_blank" rel="noopener" class="full-size">{img_tag}</a>'
        return match.group(0)

    return LINKABLE_IMG_REGEX.sub(_wrap, html)


def extract_and_save_inline_images(html: str, article_id) -> str:
    """
    Finds base64 data-URI <img> tags left over from Google Docs' HTML export
    (Drive embeds pasted-in images this way), decodes + compresses each one
    through the same pipeline as uploaded ImageFields, saves it as a real
    file under MEDIA_ROOT/article_body_images/<article_id>/<hash>.<ext>, and
    rewrites the <img src> to point at the saved file's URL.

    Content-hashed filenames make re-fetching idempotent: re-running this on
    an unchanged image produces the same path, so no duplicate files pile up.
    """
    def _replace(match):
        before_attrs, b64data, after_attrs = match.group(1), match.group('data'), match.group(3)
        try:
            raw = base64.b64decode(b64data)
        except Exception:
            logger.warning("Failed to decode an inline image in doc export; left as base64.")
            return match.group(0)

        compressed, ext = _compress_image_bytes(
            raw,
            max_px=BODY_IMAGE_MAX_PX,
            quality=BODY_IMAGE_QUALITY,
            # The editor already resized these; don't re-encode them again.
            keep_under_bytes=5_000_000,
        )
        if compressed is None:
            logger.warning("Failed to compress an inline image in doc export; left as base64.")
            return match.group(0)

        digest = hashlib.sha1(compressed).hexdigest()[:16]
        path = f"article_body_images/{article_id}/{digest}{ext}"
        if not default_storage.exists(path):
            default_storage.save(path, ContentFile(compressed))
        url = default_storage.url(path)
        return f'<img{before_attrs}src="{url}"{after_attrs}>'

    return INLINE_IMG_REGEX.sub(_replace, html)


def fetch_doc_and_parse_metadata(file_id: str, article_id=None):
    """
    Export a Google Doc as HTML, parse the top lines for metadata,
    and return (metadata_dict, cleaned_html).
    metadata_dict keys: title, writer, date, issue_number, article_type
    """
    credentials = service_account.Credentials.from_service_account_file(
        settings.GOOGLE_CREDENTIALS_FILE,
        scopes=settings.GOOGLE_API_SCOPES
    )
    drive_service = build('drive', 'v3', credentials=credentials)

    # 1. Export as HTML
    try:
        exported = drive_service.files().export(
            fileId=file_id, 
            mimeType='text/html'
        ).execute()
        if isinstance(exported, bytes):
            exported = exported.decode('utf-8', errors='replace')
    except Exception as e:
        logger.error(f"Failed to fetch doc {file_id}: {e}")
        return {}, ""

    # 2. Prepare metadata defaults
    metadata = {
        'title': '',
        'writer': '',
        'date': None,
        'issue_number': None,
        'article_type': ''
    }

    # 3. Extract the first ~5 paragraphs to parse Title, Writer, etc.
    paragraphs = re.split(r"</p>\s*<p[^>]*>", exported, maxsplit=5)
    text_lines = []
    for p in paragraphs[:6]:
        # remove remaining HTML tags
        clean_line = re.sub(r"<.*?>", "", p)
        # split on line breaks
        for line in clean_line.strip().splitlines():
            text_lines.append(line.strip())

    # 4. Match each line against our patterns
    for line in text_lines:
        if TITLE_REGEX.match(line):
            metadata['title'] = TITLE_REGEX.match(line).group(1)
        elif WRITER_REGEX.match(line):
            metadata['writer'] = WRITER_REGEX.match(line).group(1)
        elif DATE_REGEX.match(line):
            dt = DATE_REGEX.match(line).group(1)
            # Attempt to parse date "9/3/24"
            try:
                metadata['date'] = datetime.strptime(dt, "%m/%d/%y").date()
            except ValueError:
                logger.warning(f"Could not parse date '{dt}', storing as raw string.")
                metadata['date'] = dt  # store as string if parse fails
        elif ISSUE_REGEX.match(line):
            metadata['issue_number'] = int(ISSUE_REGEX.match(line).group(1))
        elif TYPE_REGEX.match(line):
            metadata['article_type'] = TYPE_REGEX.match(line).group(1)

    # 5. Strip out unwanted wrapper tags & inline styling
    cleaned_html = strip_gdoc_html(exported)

    # 6. Pull any pasted-in images out of the base64 blobs Google embeds and
    # save them as real, compressed files instead.
    cleaned_html = extract_and_save_inline_images(cleaned_html, article_id or file_id)

    return metadata, cleaned_html