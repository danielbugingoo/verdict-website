# newsletter/models.py

import io
import re
from django.core.files.base import ContentFile
from django.db import models
from django.utils.text import slugify
from PIL import Image, ImageOps

DOC_URL_REGEX = re.compile(r'/document/d/([^/]+)/')

MAX_IMAGE_PX = 1200

# Images inside article text (photo essays etc.) get a gentler limit: 2560px
# is sharper than any screen shows in the article column. The editor resizes
# to this in the browser before upload (static/newsletter/admin/editor.js).
BODY_IMAGE_MAX_PX = 2560
BODY_IMAGE_QUALITY = 90
# Full-resolution copy that article photos link to (click to view full size).
FULL_IMAGE_QUALITY = 95

def _compress_image_bytes(raw_bytes, max_px=MAX_IMAGE_PX, quality=82, keep_under_bytes=300_000):
    """
    Resize/re-encode raw image bytes. Returns (bytes, ext) where ext is
    '.jpg' or '.png'. Returns (None, None) if the bytes can't be read as
    an image. Images already within max_px and under keep_under_bytes are
    returned unchanged.
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        orig_format = img.format or 'JPEG'
        if img.width <= max_px and img.height <= max_px and len(raw_bytes) < keep_under_bytes:
            return raw_bytes, ('.png' if orig_format == 'PNG' else '.jpg')
        # Re-encoding drops EXIF, so apply camera rotation to the pixels first.
        img = ImageOps.exif_transpose(img)
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        if orig_format == 'PNG' and img.mode in ('RGBA', 'P'):
            fmt = 'PNG'
        else:
            fmt = 'JPEG'
            if img.mode != 'RGB':
                img = img.convert('RGB')
        buf = io.BytesIO()
        save_kwargs = {'optimize': True}
        if fmt == 'JPEG':
            save_kwargs['quality'] = quality
        img.save(buf, format=fmt, **save_kwargs)
        return buf.getvalue(), ('.jpg' if fmt == 'JPEG' else '.png')
    except Exception:
        return None, None


def compress_image(image_field, max_px=MAX_IMAGE_PX):
    """Resize and compress an ImageField in-place. Skips if already small enough."""
    if not image_field:
        return
    try:
        image_field.open('rb')
        raw = image_field.read()
    except Exception:
        return
    compressed, ext = _compress_image_bytes(raw, max_px=max_px)
    if compressed is None or compressed is raw:
        return
    name = re.sub(r'\.[^.]+$', ext, image_field.name)
    image_field.save(name, ContentFile(compressed), save=False)

class Author(models.Model):
    name     = models.CharField(max_length=255, unique=True)
    slug     = models.SlugField(max_length=255, unique=True, blank=True)
    bio      = models.TextField(blank=True)
    headshot = models.ImageField(upload_to='authors/', blank=True)

    # Article type choices
    ROLES = [
        ('director', 'Director'),
        ('lead editor', 'Lead Editor'),
        ('lead publisher', 'Lead Publisher'),
        ('treasurer', 'Treasurer'),
        ('social chair', 'Social Chair'),
        ('writer', 'Writer'),
        ('contributor', 'Contributor'),
    ]
    role = models.CharField(
        max_length=50,
        choices=ROLES,
        default='contributor'
    )

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:255]
        old_headshot = Author.objects.filter(pk=self.pk).values_list('headshot', flat=True).first() if self.pk else None
        super().save(*args, **kwargs)
        if self.headshot and self.headshot.name != old_headshot:
            compress_image(self.headshot, max_px=600)

    def __str__(self):
        return self.name


class Article(models.Model):
    # Basic metadata
    title = models.CharField(max_length=255, blank=True)
    writer = models.CharField(max_length=255, blank=True)
    date = models.DateField(null=True, blank=True)

    # Volume/Issue structure
    volume_number = models.PositiveIntegerField(default=1, help_text="Volume number")
    issue_number  = models.PositiveIntegerField(default=1, help_text="Issue number")

    # Short version of the title for URL slugs
    short_title = models.SlugField(
        max_length=100,
        blank=True,
        help_text="Short URL-friendly version of the title"
    )

    # if the title's an image (advisory opinion, cross examination)
    title_image = models.ImageField(upload_to='article_titles/', blank=True, null=True)

    # Preview text for homepage / listings
    preview_text = models.TextField(
        blank=True,
        help_text="Short preview or intro for the article"
    )

    # Preview image for homepage (different from title_image)
    preview_image = models.ImageField(
        upload_to='article_previews/',
        blank=True,
        null=True,
        help_text="Image to show on the current issue homepage"
    )

    hide_image_border = models.BooleanField(default=False)

    # Display order for current issue page
    display_order = models.IntegerField(
        default=0,
        help_text="Order to display on current issue page (lower numbers appear first)"
    )

    # Article type choices
    ARTICLE_TYPES = [
        ('op-ed', 'Op-Ed'),
        ('news', 'News'),
        ('features', 'Features'),
        ('other', 'Other'),
    ]
    article_type = models.CharField(
        max_length=50,
        choices=ARTICLE_TYPES,
        default='news'
    )

    # Link to one or more Author objects
    authors = models.ManyToManyField(
        Author,
        blank=True,
        related_name='articles'
    )

    # Google Doc fields
    doc_url      = models.TextField(
        blank=True,
        help_text="Optional. A Google Doc to import the article text from, e.g. https://docs.google.com/document/d/XXXXXXXX/edit"
    )
    doc_id       = models.CharField(
        max_length=255,
        blank=True,
        help_text="Auto-filled from the link above. You shouldn't need to edit this."
    )
    content_html = models.TextField(
        blank=True,
        help_text="The article text shown on the article page.",
    )

    is_current_issue = models.BooleanField(default=False, db_index=True)

    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.doc_url:
            match = DOC_URL_REGEX.search(self.doc_url)
            if match and match.group(1) != self.doc_id:
                self.doc_id = match.group(1)

        if not self.short_title:
            if self.title and self.title.strip():
                self.short_title = slugify(self.title)[:100]
            else:
                self.short_title = "untitled"

        if self.pk:
            old = Article.objects.filter(pk=self.pk).values_list('preview_image', 'title_image').first()
            old_preview, old_title = old if old else (None, None)
        else:
            old_preview = old_title = None

        super().save(*args, **kwargs)

        if self.preview_image and self.preview_image.name != old_preview:
            compress_image(self.preview_image)
        if self.title_image and self.title_image.name != old_title:
            compress_image(self.title_image)

    def __str__(self):
        return f"{self.title or 'Untitled'} (Vol {self.volume_number}, Issue {self.issue_number})"

    def get_absolute_url(self):
        from django.urls import reverse
        # Uses the new route by volume, issue, and short_title
        return reverse(
            'show_article_by_volume_issue_title',
            args=[self.volume_number, self.issue_number, self.short_title]
        )