import csv

from django.contrib import admin, messages
from django.http import HttpResponse
from django.utils import timezone

from . import access, emails
from .models import DownloadEvent, TrialLead


def _cell(value):
    """Text that a spreadsheet would run as a formula is kept as text."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


class DownloadInline(admin.TabularInline):
    model = DownloadEvent
    extra = 0
    can_delete = False
    fields = readonly_fields = ("created_at", "version", "filename", "ip_address")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(TrialLead)
class TrialLeadAdmin(admin.ModelAdmin):
    date_hierarchy = "created_at"
    list_display = (
        "created_at", "reference", "company", "full_name", "email", "phone", "country",
        "device_count", "hypervisor", "status", "download_count",
    )
    list_filter = ("status", "device_count", "hypervisor", "marketing_opt_in", "country")
    search_fields = ("reference", "email", "email_domain", "company", "first_name", "last_name", "phone")
    readonly_fields = (
        "reference", "created_at", "updated_at", "email_domain", "accepted_terms_at", "terms_version",
        "link_sent_at", "link_sent_count", "verified_at", "access_expires_at", "sales_notified_at",
        "download_count", "last_download_at", "source", "ip_address", "user_agent",
    )
    fieldsets = (
        ("Follow-up", {"fields": ("status", "notes")}),
        ("Contact", {"fields": ("first_name", "last_name", "email", "phone", "company", "job_title", "country")}),
        ("Evaluation", {"fields": ("device_count", "hypervisor", "current_tool")}),
        ("Consent", {"fields": ("accepted_terms_at", "terms_version", "marketing_opt_in")}),
        ("Download access", {"fields": (
            "reference", "link_sent_at", "link_sent_count", "verified_at", "access_expires_at",
            "download_count", "last_download_at", "sales_notified_at",
        )}),
        ("Origin", {"fields": ("created_at", "updated_at", "source", "ip_address", "user_agent")}),
    )
    inlines = (DownloadInline,)
    actions = ("export_csv", "send_new_link")

    @admin.action(description="Export selected requests as CSV")
    def export_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="zenplus-trial-requests-%s.csv"' % (
            timezone.now().strftime("%Y%m%d")
        )
        response.write("﻿")  # spreadsheet programs read the file as UTF-8
        columns = (
            "reference", "created_at", "status", "first_name", "last_name", "email", "phone", "company",
            "job_title", "country", "device_count", "hypervisor", "current_tool", "marketing_opt_in",
            "verified_at", "download_count", "last_download_at", "source",
        )
        writer = csv.writer(response)
        writer.writerow(columns)
        for lead in queryset.iterator():
            writer.writerow([_cell(getattr(lead, name)) for name in columns])
        return response

    @admin.action(description="Send a new download link (cancels earlier links)")
    def send_new_link(self, request, queryset):
        sent = 0
        for lead in queryset:
            token = access.issue_link(lead)
            lead.save()
            sent += bool(emails.send_download_link(lead, access.access_url(lead, token)))
        self.message_user(request, "%d of %d links sent." % (sent, queryset.count()), messages.INFO)
