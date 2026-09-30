# newsletter/utils.py
import base64
import hashlib
import logging
import re
from datetime import datetime

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from googleapiclient.discovery import build
from google.oauth2 import service_account

from .models import _compress_image_bytes

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

    # BEFORE removing styles, convert Google's italic/bold spans to semantic HTML
    # Convert <span style="...font-style:italic...">text</span> to <em>text</em>
    # Using non-greedy (.*?) to capture content including nested tags
    raw_html = re.sub(
        r'<span\s+style="[^"]*font-style:\s*italic[^"]*">(.*?)</span>',
        r'<em>\1</em>',
        raw_html,
        flags=re.IGNORECASE | re.DOTALL
    )

    # Convert <span style="...font-weight:700...">text</span> to <strong>text</strong>
    # Using non-greedy (.*?) to capture content including nested tags
    raw_html = re.sub(
        r'<span\s+style="[^"]*font-weight:\s*(?:700|bold)[^"]*">(.*?)</span>',
        r'<strong>\1</strong>',
        raw_html,
        flags=re.IGNORECASE | re.DOTALL
    )

    # Remove inline style attributes EXCEPT text-align
    # Keep text-align styles but remove everything else
    def preserve_text_align(match):
        style_content = match.group(1)
        # Check if there's a text-align property
        text_align_match = re.search(r'text-align:\s*[^;]+', style_content, re.IGNORECASE)
        if text_align_match:
            return f' style="{text_align_match.group(0)}"'
        return ''

    raw_html = re.sub(r'\s+style="([^"]*)"', preserve_text_align, raw_html, flags=re.IGNORECASE)

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

    return raw_html.strip()


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

        compressed, ext = _compress_image_bytes(raw)
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