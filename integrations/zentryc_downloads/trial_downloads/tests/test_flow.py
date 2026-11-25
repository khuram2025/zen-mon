import re
import time
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail, signing
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from trial_downloads import access, forms
from trial_downloads.models import DownloadEvent, TrialLead

REQUEST = "/downloads/trial/"
SENT = "/downloads/trial/sent/"
BASE = {"SITE_URL": "https://zentryc.example", "CHECK_MX": False, "NOTIFY": ["sales@zentryc.example"]}


def settings_with(**change):
    return override_settings(TRIAL_DOWNLOADS=dict(BASE, **change))


def stamp(seconds_ago=30):
    return signing.dumps(int(time.time()) - seconds_ago, salt=forms._STAMP_SALT)


def details(**change):
    data = {
        "first_name": "Sara", "last_name": "Ali", "email": "Sara.Ali@Example-Corp.com",
        "company": "Example Corp", "job_title": "Network Manager", "country": "SA",
        "phone": "050 123 4567", "device_count": "101-500", "hypervisor": "vmware_esxi",
        "current_tool": "PRTG", "accept_terms": "on", "website": "", "opened": stamp(), "src": "utm_source=test",
    }
    data.update(change)
    return data


@override_settings(TRIAL_DOWNLOADS=BASE)
class FlowCase(TestCase):
    def setUp(self):
        cache.clear()

    def submit(self, **change):
        return self.client.post(REQUEST, details(**change))

    def link(self, message=None):
        body = (message or mail.outbox[-1]).body
        return re.search(r"https://zentryc\.example(/downloads/trial/d/\S+/)", body).group(1)

    def confirmed_lead(self):
        self.submit()
        path = self.link()
        self.client.get(path)
        return TrialLead.objects.get(), path


class RequestTests(FlowCase):
    def test_form_page(self):
        response = self.client.get(REQUEST + "?platform=vmware&utm_source=linkedin", HTTP_REFERER="https://zentryc.example/pricing/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="email"')
        self.assertContains(response, "platform=vmware")
        self.assertContains(response, "from https://zentryc.example/pricing/")
        self.assertIn("noindex", response["X-Robots-Tag"])
        self.assertIn("no-cache", response["Cache-Control"])

    def test_valid_request_stores_the_person_and_emails_one_link(self):
        response = self.submit()
        self.assertRedirects(response, SENT)
        lead = TrialLead.objects.get()
        self.assertEqual(
            (lead.email, lead.email_domain, lead.phone, lead.company, lead.country, lead.status),
            ("sara.ali@example-corp.com", "example-corp.com", "+966501234567", "Example Corp", "SA", "new"),
        )
        self.assertTrue(lead.reference.startswith("ZT-"))
        self.assertEqual((lead.terms_version, lead.marketing_opt_in, lead.source), ("2026-04-05", False, "utm_source=test"))
        self.assertIsNotNone(lead.accepted_terms_at)
        self.assertIsNone(lead.verified_at)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual((message.to, message.subject), (["sara.ali@example-corp.com"], "Your ZenPlus trial download"))
        self.assertIn(lead.reference, message.body)
        self.assertIn(self.link(), message.alternatives[0][0])

    def test_every_required_detail_is_required(self):
        for name in ("first_name", "last_name", "email", "company", "job_title", "country", "phone",
                     "device_count", "hypervisor", "accept_terms"):
            response = self.submit(**{name: ""})
            self.assertEqual(response.status_code, 422, name)
            self.assertIn(name, response.context["form"].errors, name)
        self.assertEqual(TrialLead.objects.count(), 0)
        self.assertEqual(mail.outbox, [])

    def test_refusals_explain_themselves(self):
        cases = (
            ({"email": "sara@gmail.com"}, "Use your work email address"),
            ({"email": "sara@mailinator.com"}, "Temporary email addresses"),
            ({"phone": "12345"}, "with the country code"),
            ({"company": "n/a"}, "name of your company"),
            ({"country": "XX"}, "Select a valid choice"),
        )
        for change, text in cases:
            response = self.submit(**change)
            self.assertContains(response, text, status_code=422)
        self.assertEqual(TrialLead.objects.count(), 0)

    def test_domain_that_cannot_receive_mail_is_refused(self):
        with settings_with(CHECK_MX=True), mock.patch("trial_downloads.validators.domain_accepts_mail", return_value=False):
            self.assertContains(self.submit(), "cannot receive email", status_code=422)
        self.assertEqual(TrialLead.objects.count(), 0)

    def test_automated_posts_get_the_normal_answer_and_nothing_else(self):
        for change in ({"website": "https://spam.example"}, {"opened": stamp(0)}, {"opened": "forged"}, {"opened": ""}):
            self.assertRedirects(self.submit(**change), SENT)
        self.assertEqual(TrialLead.objects.count(), 0)
        self.assertEqual(mail.outbox, [])

    def test_blocked_country(self):
        with settings_with(BLOCKED_COUNTRIES=["SA"]):
            self.assertContains(self.submit(), "cannot offer this download in your country", status_code=422)
        self.assertEqual(TrialLead.objects.count(), 0)

    def test_asking_again_keeps_one_record_and_cancels_the_old_link(self):
        self.submit()
        first = self.link()
        reference = TrialLead.objects.get().reference
        self.submit(email="SARA.ALI@example-corp.com", company="Example Corporation", phone="+966 50 765 4321")
        lead = TrialLead.objects.get()
        self.assertEqual((lead.reference, lead.company, lead.phone, lead.link_sent_count),
                         (reference, "Example Corporation", "+966507654321", 2))
        second = self.link()
        self.assertNotEqual(first, second)
        self.assertEqual(self.client.get(first).status_code, 404)
        self.assertEqual(self.client.get(second).status_code, 200)

    def test_one_address_is_not_mailed_repeatedly(self):
        for _ in range(5):
            self.assertRedirects(self.submit(), SENT)
        self.assertEqual(len(mail.outbox), 3)

    def test_one_network_cannot_flood_the_form(self):
        with settings_with(RATE_IP_PER_HOUR=2):
            for index in range(2):
                self.assertRedirects(self.submit(email="user%d@example-corp.com" % index), SENT)
            self.assertContains(self.submit(email="user9@example-corp.com"), "Too many requests", status_code=422)
        self.assertEqual(TrialLead.objects.count(), 2)

    def test_failed_email_is_reported_with_the_reference(self):
        with mock.patch("django.core.mail.message.EmailMessage.send", side_effect=OSError("smtp down")):
            with self.assertLogs("trial_downloads.emails", level="ERROR") as logs:
                response = self.submit()
        lead = TrialLead.objects.get()
        self.assertContains(response, "could not send the email", status_code=422)
        self.assertContains(response, lead.reference, status_code=422)
        self.assertNotIn(lead.email, "".join(logs.output))


class AccessTests(FlowCase):
    def test_opening_the_link_confirms_the_address_and_tells_sales_once(self):
        self.submit()
        path = self.link()
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Thank you, Sara")
        self.assertContains(response, path + "vmware/")
        self.assertContains(response, path + "hyperv/")
        self.assertEqual((response["Cache-Control"], response["Referrer-Policy"]), ("no-store, private", "no-referrer"))
        self.assertIn("noindex", response["X-Robots-Tag"])
        lead = TrialLead.objects.get()
        self.assertEqual(lead.status, "confirmed")
        self.assertIsNotNone(lead.verified_at)
        self.assertAlmostEqual(lead.access_expires_at, timezone.now() + timedelta(days=7), delta=timedelta(minutes=1))
        notice = mail.outbox[-1]
        self.assertEqual((notice.to, notice.reply_to), (["sales@zentryc.example"], ["sara.ali@example-corp.com"]))
        for text in ("Example Corp", "+966501234567", "Saudi Arabia", "101 to 500", "VMware ESXi", "PRTG", lead.reference):
            self.assertIn(text, notice.body + notice.subject)
        self.client.get(path)
        self.assertEqual(len(mail.outbox), 2)  # the link, one notice

    def test_no_sales_recipients_configured(self):
        with settings_with(NOTIFY=[]):
            self.confirmed_lead()
        self.assertEqual(len(mail.outbox), 1)

    def test_forged_and_foreign_links_are_not_found(self):
        lead, path = self.confirmed_lead()
        token = path.split("/")[-2]
        for bad in (token[:-3] + "abc", signing.dumps({"l": str(lead.pk), "n": "other"}, salt=access._SALT),
                    signing.dumps({"l": str(lead.pk), "n": lead.access_nonce}, salt="another.salt"),
                    signing.dumps({"l": "not-a-uuid", "n": "x"}, salt=access._SALT), "x" * 40):
            self.assertEqual(self.client.get("/downloads/trial/d/%s/" % bad).status_code, 404, bad)
            self.assertEqual(self.client.get("/downloads/trial/d/%s/vmware/" % bad).status_code, 404, bad)

    def test_link_must_be_opened_in_time(self):
        self.submit()
        TrialLead.objects.update(link_sent_at=timezone.now() - timedelta(hours=73))
        response = self.client.get(self.link())
        self.assertContains(response, "has expired", status_code=410)
        self.assertContains(response, REQUEST, status_code=410)
        self.assertIsNone(TrialLead.objects.get().verified_at)
        self.assertEqual(len(mail.outbox), 1)

    def test_access_period_ends_and_a_new_request_reopens_it(self):
        lead, path = self.confirmed_lead()
        TrialLead.objects.update(access_expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.get(path).status_code, 410)
        self.assertEqual(self.client.get(path + "vmware/").status_code, 410)
        self.submit()
        self.assertEqual(self.client.get(self.link()).status_code, 200)
        lead.refresh_from_db()
        self.assertGreater(lead.access_expires_at, timezone.now())


class FileTests(FlowCase):
    def test_file_is_handed_to_nginx_and_recorded(self):
        lead, path = self.confirmed_lead()
        response = self.client.get(path + "vmware/", HTTP_USER_AGENT="Browser/1.0")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Accel-Redirect"], "/_protected/zenplus-appliance/1.25.2/zenplus-appliance-1.25.2-vmware.ova")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="zenplus-appliance-1.25.2-vmware.ova"')
        self.assertEqual(response["Cache-Control"], "no-store, private")
        self.assertEqual(response.content, b"")
        event = DownloadEvent.objects.get()
        self.assertEqual((event.lead, event.version, event.artifact, event.user_agent), (lead, "1.25.2", "vmware", "Browser/1.0"))
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.download_count), ("downloaded", 1))
        self.assertIsNotNone(lead.last_download_at)

    def test_resumed_and_repeated_requests_are_one_download(self):
        lead, path = self.confirmed_lead()
        self.client.get(path + "vmware/")
        self.client.get(path + "vmware/", HTTP_RANGE="bytes=1048576-")
        self.client.head(path + "vmware/")
        self.client.get(path + "vmware/", HTTP_RANGE="bytes=0-")
        self.client.get(path + "vmware/")
        self.assertEqual(DownloadEvent.objects.count(), 1)
        DownloadEvent.objects.update(created_at=timezone.now() - timedelta(minutes=11))
        self.client.get(path + "vmware/")
        self.client.get(path + "hyperv/")
        lead.refresh_from_db()
        self.assertEqual((DownloadEvent.objects.count(), lead.download_count), (3, 3))

    def test_unknown_file_and_download_limit(self):
        lead, path = self.confirmed_lead()
        self.assertEqual(self.client.get(path + "kvm/").status_code, 404)
        TrialLead.objects.update(download_count=12)
        response = self.client.get(path + "vmware/")
        self.assertContains(response, "download limit", status_code=410)
        self.assertNotIn("X-Accel-Redirect", response)
        self.assertEqual(DownloadEvent.objects.count(), 0)

    def test_sales_status_is_never_moved_back(self):
        lead, path = self.confirmed_lead()
        TrialLead.objects.update(status="qualified")
        self.client.get(path + "vmware/")
        self.assertEqual(TrialLead.objects.get().status, "qualified")

    def test_first_request_may_be_the_file_itself(self):
        self.submit()
        response = self.client.get(self.link() + "hyperv/")
        self.assertIn("X-Accel-Redirect", response)
        self.assertIsNotNone(TrialLead.objects.get().verified_at)
        self.assertEqual(mail.outbox[-1].to, ["sales@zentryc.example"])

    def test_development_serving_without_nginx(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            os.mkdir(os.path.join(root, "1.25.2"))
            lead, path = self.confirmed_lead()
            with settings_with(SERVE_ROOT=root):
                self.assertEqual(self.client.get(path + "vmware/").status_code, 404)
                with open(os.path.join(root, "1.25.2", "zenplus-appliance-1.25.2-vmware.ova"), "wb") as handle:
                    handle.write(b"image")
                response = self.client.get(path + "vmware/")
                self.assertEqual(b"".join(response.streaming_content), b"image")


class PublicPageTests(FlowCase):
    def test_public_pages_never_link_an_image_directly(self):
        for path in ("/downloads/", "/downloads/zenplus-appliance/1.25.2/", REQUEST, SENT):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            for image in re.findall(r'href="([^"]+)"', response.content.decode()):
                self.assertFalse(image.endswith((".ova", ".zip", ".vhdx", ".qcow2")), (path, image))
        release = self.client.get("/downloads/zenplus-appliance/1.25.2/")
        self.assertContains(release, "/downloads/trial/?platform=vmware")
        self.assertContains(release, "7 November 2026")
        self.assertContains(release, "zenplus-appliance-1.25.2-vmware.ova.sha256.sig")
        self.assertContains(release, "e1b712274387daa83f835a3e32ad3b4038a12ba9037a6ade544b55b31822c996")
        self.assertContains(self.client.get("/downloads/"), "/downloads/zenplus-agent/")

    def test_release_addresses(self):
        self.assertRedirects(self.client.get("/downloads/zenplus-appliance/"), "/downloads/zenplus-appliance/1.25.2/")
        self.assertEqual(self.client.get("/downloads/zenplus-appliance/9.9.9/").status_code, 404)

    def test_only_reading_methods(self):
        self.assertEqual(self.client.post("/downloads/").status_code, 405)
        self.assertEqual(self.client.put(REQUEST).status_code, 405)
        lead, path = self.confirmed_lead()
        self.assertEqual(self.client.post(path + "vmware/").status_code, 405)


class AdminAndCleanupTests(FlowCase):
    def test_export_keeps_formula_text_as_text(self):
        get_user_model().objects.create_superuser("owner", "owner@zentryc.example", "pw-for-tests")
        self.client.login(username="owner", password="pw-for-tests")
        self.submit(company="=HYPERLINK(1) Ltd")
        lead = TrialLead.objects.get()
        response = self.client.post(
            reverse("admin:trial_downloads_triallead_changelist"),
            {"action": "export_csv", "_selected_action": [str(lead.pk)]},
        )
        text = response.content.decode("utf-8-sig")
        self.assertIn("'=HYPERLINK(1) Ltd", text)
        self.assertIn("'+966501234567", text)
        self.assertIn("sara.ali@example-corp.com", text)
        self.assertEqual(self.client.get(reverse("admin:trial_downloads_triallead_change", args=[lead.pk])).status_code, 200)

    def test_new_link_from_admin(self):
        get_user_model().objects.create_superuser("owner", "owner@zentryc.example", "pw-for-tests")
        self.client.login(username="owner", password="pw-for-tests")
        lead, old = self.confirmed_lead()
        self.client.post(reverse("admin:trial_downloads_triallead_changelist"),
                         {"action": "send_new_link", "_selected_action": [str(lead.pk)]})
        self.client.logout()
        self.assertEqual(self.client.get(old).status_code, 404)
        self.assertEqual(self.client.get(self.link()).status_code, 200)

    def test_cleanup(self):
        self.submit(email="old-unconfirmed@example-corp.com")
        self.submit(email="old-confirmed@example-corp.com")
        confirmed_path = self.link()
        self.client.get(confirmed_path)
        self.client.get(confirmed_path + "vmware/", REMOTE_ADDR="198.51.100.7")
        self.submit(email="recent@example-corp.com")
        long_ago = timezone.now() - timedelta(days=120)
        TrialLead.objects.exclude(email__startswith="recent").update(created_at=long_ago)
        DownloadEvent.objects.update(created_at=long_ago)

        out = StringIO()
        call_command("purge_trial_requests", "--dry-run", stdout=out)
        self.assertIn("DRY RUN", out.getvalue())
        self.assertEqual(TrialLead.objects.count(), 3)

        call_command("purge_trial_requests", stdout=out)
        self.assertEqual(sorted(TrialLead.objects.values_list("email", flat=True)),
                         ["old-confirmed@example-corp.com", "recent@example-corp.com"])
        old = TrialLead.objects.get(email__startswith="old-confirmed")
        self.assertEqual((old.ip_address, old.user_agent, old.phone), (None, "", "+966501234567"))
        self.assertIsNone(DownloadEvent.objects.get().ip_address)
        self.assertIsNotNone(TrialLead.objects.get(email__startswith="recent").ip_address)
