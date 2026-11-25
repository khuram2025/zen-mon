import secrets
import uuid

from django.db import models
from django.utils import timezone

from .countries import COUNTRIES

DEVICE_BANDS = (
    ("1-25", "Up to 25"),
    ("26-100", "26 to 100"),
    ("101-500", "101 to 500"),
    ("501-2000", "501 to 2,000"),
    ("2000+", "More than 2,000"),
)

HYPERVISORS = (
    ("vmware_esxi", "VMware ESXi / vSphere"),
    ("vmware_workstation", "VMware Workstation or Fusion"),
    ("hyperv", "Microsoft Hyper-V"),
    ("kvm", "KVM / Proxmox / Nutanix"),
    ("other", "Other or not decided"),
)

STATUSES = (
    ("new", "New, email not confirmed"),
    ("confirmed", "Email confirmed"),
    ("downloaded", "Downloaded"),
    ("contacted", "Contacted"),
    ("qualified", "Qualified opportunity"),
    ("customer", "Customer"),
    ("closed", "Closed, not a fit"),
)

# Statuses the site sets by itself. Once sales moves a lead further, the site
# never moves it back.
AUTOMATIC_STATUSES = ("new", "confirmed", "downloaded")

_REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I


def new_reference():
    return "ZT-" + "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(7))


def new_nonce():
    return secrets.token_urlsafe(12)


class TrialLead(models.Model):
    """One person who asked for the ZenPlus trial appliance."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=12, unique=True, default=new_reference, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    # Stored with a lower-case domain; one row per address (compared without case).
    email = models.EmailField(max_length=254, unique=True)
    email_domain = models.CharField(max_length=253, db_index=True)
    phone = models.CharField(max_length=20, help_text="E.164, for example +966501234567")
    company = models.CharField(max_length=200)
    job_title = models.CharField(max_length=120)
    country = models.CharField(max_length=2, choices=COUNTRIES)
    device_count = models.CharField("planned devices", max_length=12, choices=DEVICE_BANDS)
    hypervisor = models.CharField(max_length=24, choices=HYPERVISORS)
    current_tool = models.CharField("current monitoring tool", max_length=80, blank=True)

    accepted_terms_at = models.DateTimeField()
    terms_version = models.CharField(max_length=20)
    marketing_opt_in = models.BooleanField(default=False)

    status = models.CharField(max_length=12, choices=STATUSES, default="new", db_index=True)
    notes = models.TextField(blank=True, help_text="Internal. Never shown to the visitor.")

    # The download link in the email carries this value; changing it cancels
    # every link sent before.
    access_nonce = models.CharField(max_length=24, default=new_nonce, editable=False)
    link_sent_at = models.DateTimeField(null=True, blank=True)
    link_sent_count = models.PositiveIntegerField(default=0)
    verified_at = models.DateTimeField("email first confirmed", null=True, blank=True)
    access_expires_at = models.DateTimeField(null=True, blank=True)
    sales_notified_at = models.DateTimeField(null=True, blank=True)
    download_count = models.PositiveIntegerField(default=0)
    last_download_at = models.DateTimeField(null=True, blank=True)

    source = models.CharField(max_length=300, blank=True, help_text="Campaign parameters and referring page")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "trial request"

    def __str__(self):
        return "%s %s, %s (%s)" % (self.first_name, self.last_name, self.company, self.reference)

    @property
    def full_name(self):
        return ("%s %s" % (self.first_name, self.last_name)).strip()

    def advance_status(self, status):
        """Move forward among the automatic statuses; never undo a sales decision."""
        if self.status in AUTOMATIC_STATUSES and (
            AUTOMATIC_STATUSES.index(status) > AUTOMATIC_STATUSES.index(self.status)
        ):
            self.status = status


class DownloadEvent(models.Model):
    """A file download started from a personal download page."""

    lead = models.ForeignKey(TrialLead, on_delete=models.CASCADE, related_name="downloads")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    version = models.CharField(max_length=20)
    artifact = models.CharField(max_length=40)
    filename = models.CharField(max_length=200)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return "%s %s" % (self.filename, self.created_at.strftime("%Y-%m-%d %H:%M"))
