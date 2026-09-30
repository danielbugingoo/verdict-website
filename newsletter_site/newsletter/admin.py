# newsletter/admin.py

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils.html import format_html

from .models import Author, Article, DOC_URL_REGEX
from .utils import fetch_doc_and_parse_metadata


def _apply_fetched_content(article, metadata, html_content):
    """Apply fetched metadata + HTML onto an Article instance and save it.

    Shared by the list-view bulk action and the per-article "Fetch from
    Google Doc" button, so both stay in sync.
    """
    article.title = metadata.get('title') or article.title
    article.writer = metadata.get('writer') or article.writer

    date_val = metadata.get('date')
    if date_val and hasattr(date_val, "year"):
        article.date = date_val

    if metadata.get('issue_number') is not None:
        article.issue_number = metadata['issue_number']
    if metadata.get('article_type'):
        article.article_type = metadata['article_type']

    article.content_html = html_content
    article.save()


@admin.action(description="Add to Current Issue")
def mark_as_current(modeladmin, request, queryset):
    updated = queryset.update(is_current_issue=True)
    messages.success(request, f"{updated} article(s) marked as current.")

@admin.action(description="Add to Past Issues")
def mark_as_past(modeladmin, request, queryset):
    updated = queryset.update(is_current_issue=False)
    messages.success(request, f"{updated} article(s) marked as past.")

@admin.action(description="Fetch Doc & Parse Metadata")
def fetch_doc_and_metadata(modeladmin, request, queryset):
    for article in queryset:
        if not article.doc_id:
            messages.error(request, f"Article {article} has no doc_id to fetch.")
            continue
        metadata, html_content = fetch_doc_and_parse_metadata(article.doc_id, article_id=article.pk)
        if not html_content:
            messages.error(request, f"Could not fetch doc {article.doc_id}.")
            continue
        _apply_fetched_content(article, metadata, html_content)
        messages.success(request, f"Fetched & updated {article.title}.")


class ArticleAdminForm(forms.ModelForm):
    """Renders the auto-generated fields as visibly read-only so editors
    don't accidentally hand-edit something that gets overwritten on the
    next fetch. This is a UI guardrail, not a hard server-side lock."""

    class Meta:
        model = Article
        fields = "__all__"
        widgets = {
            "content_html": forms.Textarea(attrs={
                "readonly": "readonly",
                "rows": 20,
                "style": "background:#f5f5f5; font-family: monospace; font-size: 11px;",
            }),
        }


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    form = ArticleAdminForm

    list_display = (
        "title",
        "fetch_status",
        "volume_number",
        "issue_number",
        "article_type",
        "is_current_issue",
        "display_order",
        "updated_at",
    )
    list_editable = ("volume_number", "issue_number", "is_current_issue", "display_order")
    search_fields = ("title", "writer", "short_title")
    list_filter = ("article_type", "is_current_issue")

    fieldsets = (
        (None, {
            "fields": (
                "title_image",
                "title",
                "short_title",
                "writer",
                "authors",
                "preview_text",
                "preview_image",
                "hide_image_border",
                "date",
                "article_type",
                "volume_number",
                "issue_number",
                "is_current_issue",
                "display_order",
            )
        }),
        ("Article Content (Google Doc)", {
            "description": (
                "<strong>Step 1:</strong> Paste the full Google Doc link below and click "
                "<em>Save</em> at the bottom of this page.<br>"
                "<strong>Step 2:</strong> A “Fetch from Google Doc” button will appear "
                "at the top-right of this page — click it to automatically pull in the "
                "title, writer, date, and article text. You can click it again any time "
                "after editing the Google Doc to re-sync this article.<br><br>"
                "<strong>Footnotes:</strong> use Google Docs' built-in "
                "<em>Insert &rarr; Footnote</em> tool. They're picked up automatically and "
                "turned into clickable numbered links on the article page — no extra work "
                "needed here.<br>"
                "<strong>Images:</strong> just paste or drag them into the Google Doc "
                "wherever you want them to appear — they'll be pulled in and resized "
                "automatically when you fetch."
            ),
            "fields": ("doc_url",),
        }),
        ("Advanced (auto-generated, you shouldn't need this)", {
            "classes": ("collapse",),
            "fields": ("content_html",),
        }),
    )

    actions = [mark_as_current, mark_as_past, fetch_doc_and_metadata]

    def fetch_status(self, obj):
        if not obj.doc_id:
            return format_html('<span style="color:#888;">— no doc linked</span>')
        if obj.content_html.strip():
            return format_html('<span style="color:#1a7f37; font-weight:bold;">&#10003; Has content</span>')
        return format_html('<span style="color:#b45309; font-weight:bold;">&#9888; Not fetched yet</span>')
    fetch_status.short_description = "Fetch Status"

    def get_urls(self):
        custom = [
            path(
                "<int:object_id>/fetch-doc/",
                self.admin_site.admin_view(self.fetch_doc_view),
                name="newsletter_article_fetch_doc",
            ),
        ]
        return custom + super().get_urls()

    def fetch_doc_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])

        article = get_object_or_404(Article, pk=object_id)
        if not self.has_change_permission(request, article):
            raise PermissionDenied

        change_url = reverse("admin:newsletter_article_change", args=[object_id])

        # Re-derive doc_id in case doc_url was edited since the last save.
        match = DOC_URL_REGEX.search(article.doc_url or "")
        if match and match.group(1) != article.doc_id:
            article.doc_id = match.group(1)
            article.save(update_fields=["doc_id"])

        if not article.doc_id:
            messages.error(
                request,
                "No valid Google Doc link found. Paste a full Google Doc URL into "
                "'Google Doc URL', click Save, then try Fetch again."
            )
            return redirect(change_url)

        metadata, html_content = fetch_doc_and_parse_metadata(article.doc_id, article_id=article.pk)
        if not html_content:
            messages.error(
                request,
                "Could not fetch the Google Doc. Make sure it's shared with the "
                "service account and try again."
            )
            return redirect(change_url)

        _apply_fetched_content(article, metadata, html_content)
        messages.success(request, f"Fetched & updated “{article.title}” from Google Docs.")
        return redirect(change_url)


@admin.register(Author)
class AuthorAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "role", "article_count")
    prepopulated_fields = {"slug": ("name",)}
    fields = ("name", "slug", "role", "bio", "headshot")

    def article_count(self, obj):
        return obj.articles.count()
    article_count.short_description = "Articles"
