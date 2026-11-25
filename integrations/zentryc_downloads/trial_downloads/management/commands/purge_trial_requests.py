"""Keep only what the privacy policy says is kept. Run daily (cron or Celery beat)."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from trial_downloads.models import DownloadEvent, TrialLead


class Command(BaseCommand):
    help = (
        "Delete trial requests whose email was never confirmed, and remove network "
        "addresses and browser details from older records."
    )

    def add_arguments(self, parser):
        parser.add_argument("--unconfirmed-days", type=int, default=30)
        parser.add_argument("--network-detail-days", type=int, default=90)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        now = timezone.now()
        stale = TrialLead.objects.filter(
            verified_at__isnull=True, created_at__lt=now - timedelta(days=options["unconfirmed_days"])
        )
        cutoff = now - timedelta(days=options["network_detail_days"])
        leads = TrialLead.objects.filter(created_at__lt=cutoff).exclude(ip_address__isnull=True, user_agent="")
        events = DownloadEvent.objects.filter(created_at__lt=cutoff).exclude(ip_address__isnull=True, user_agent="")
        counts = (stale.count(), leads.count(), events.count())
        if not options["dry_run"]:
            stale.delete()
            leads.update(ip_address=None, user_agent="")
            events.update(ip_address=None, user_agent="")
        self.stdout.write(
            "%sunconfirmed requests deleted: %d; records stripped of network detail: %d requests, %d downloads"
            % ((("DRY RUN. " if options["dry_run"] else ""),) + counts)
        )
