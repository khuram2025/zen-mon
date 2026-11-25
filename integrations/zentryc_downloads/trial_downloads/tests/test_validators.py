from unittest import mock

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings

from trial_downloads import catalog, validators


def _code(callable_, *args):
    try:
        callable_(*args)
    except ValidationError as error:
        return error.code
    return None


@override_settings(TRIAL_DOWNLOADS={"CHECK_MX": False})
class WorkEmailTests(SimpleTestCase):
    def test_company_address_is_accepted_with_lowercase_domain(self):
        self.assertEqual(validators.validate_work_email(" Sara.Ali@Example-Corp.COM "), "Sara.Ali@example-corp.com")

    def test_personal_mailboxes_are_refused(self):
        for address in ("a@gmail.com", "a@GMAIL.com", "a@yahoo.co.uk", "a@mail.yahoo.co.uk", "a@outlook.sa"):
            self.assertEqual(_code(validators.validate_work_email, address), "free_provider", address)

    def test_personal_mailboxes_can_be_allowed(self):
        with override_settings(TRIAL_DOWNLOADS={"CHECK_MX": False, "ALLOW_FREE_EMAIL": True}):
            self.assertEqual(validators.validate_work_email("a@gmail.com"), "a@gmail.com")
            # A throw-away mailbox is refused whatever the setting.
            self.assertEqual(_code(validators.validate_work_email, "a@mailinator.com"), "disposable")

    def test_disposable_and_malformed_are_refused(self):
        self.assertEqual(_code(validators.validate_work_email, "a@sub.yopmail.com"), "disposable")
        for address in ("", "plain", "a@b", "a b@example.com", "a@@example.com"):
            self.assertEqual(_code(validators.validate_work_email, address), "invalid", address)

    def test_a_similar_looking_company_domain_is_not_a_free_provider(self):
        self.assertEqual(validators.validate_work_email("a@notgmail.com"), "a@notgmail.com")
        self.assertEqual(validators.validate_work_email("a@gmail.com.example.org"), "a@gmail.com.example.org")


@override_settings(TRIAL_DOWNLOADS={"CHECK_MX": True})
class MailServerTests(SimpleTestCase):
    def test_domain_without_mail_is_refused_and_dns_trouble_is_not(self):
        with mock.patch.object(validators, "domain_accepts_mail", return_value=False):
            self.assertEqual(_code(validators.validate_work_email, "a@no-such-company.example"), "no_mail_server")
        for answer in (True, None):  # None: the resolver could not say
            with mock.patch.object(validators, "domain_accepts_mail", return_value=answer):
                self.assertEqual(validators.validate_work_email("a@company.example"), "a@company.example")

    def test_resolver_answers(self):
        import dns.exception
        import dns.resolver

        def resolver(outcomes):
            instance = mock.Mock()
            instance.resolve.side_effect = lambda domain, record: outcomes[record]()
            return mock.patch.object(dns.resolver, "Resolver", return_value=instance)

        def raises(error):
            def fail():
                raise error()
            return fail

        mx = mock.Mock()
        mx.exchange = "mx1.company.example."
        null_mx = mock.Mock()
        null_mx.exchange = "."
        cases = [
            ({"MX": lambda: [mx]}, True),
            ({"MX": lambda: [null_mx]}, False),
            ({"MX": raises(dns.resolver.NXDOMAIN)}, False),
            ({"MX": raises(dns.exception.Timeout)}, None),
            ({"MX": raises(dns.resolver.NoAnswer), "A": lambda: ["192.0.2.1"]}, True),
            ({"MX": raises(dns.resolver.NoAnswer), "A": raises(dns.resolver.NoAnswer),
              "AAAA": raises(dns.resolver.NoAnswer)}, False),
            ({"MX": raises(dns.resolver.NoAnswer), "A": raises(dns.exception.Timeout)}, None),
        ]
        for outcomes, expected in cases:
            with resolver(outcomes):
                self.assertIs(validators.domain_accepts_mail("company.example"), expected, outcomes)


class PhoneTests(SimpleTestCase):
    def test_numbers_are_stored_in_international_form(self):
        self.assertEqual(validators.normalize_phone("+966 50 123 4567"), "+966501234567")
        self.assertEqual(validators.normalize_phone("00966 50 123 4567"), "+966501234567")
        self.assertEqual(validators.normalize_phone("050 123 4567", "SA"), "+966501234567")
        self.assertEqual(validators.normalize_phone("(212) 555-0199", "US"), "+12125550199")
        self.assertEqual(validators.normalize_phone("+44 20 7946 0958", "SA"), "+442079460958")

    def test_unreachable_numbers_are_refused(self):
        for raw, country in (("", "SA"), ("12345", "SA"), ("phone", "US"), ("+966 11", None),
                             ("050 123 4567", None), ("0000000000", "US"), ("+1 555 CALL NOW", "US")):
            self.assertEqual(_code(validators.normalize_phone, raw, country), "invalid", raw)

    def test_shape_check_without_the_phonenumbers_library(self):
        with mock.patch.object(validators, "phonenumbers", None):
            self.assertEqual(validators.normalize_phone("+966 50 123 4567"), "+966501234567")
            for raw in ("050 123 4567", "+1111111111", "+12", "+1234567890123456"):
                self.assertEqual(_code(validators.normalize_phone, raw, "SA"), "invalid", raw)


class NameTests(SimpleTestCase):
    def test_company(self):
        self.assertEqual(validators.validate_company("  Al  Noor Trading Co. "), "Al Noor Trading Co.")
        self.assertEqual(validators.validate_company("3M"), "3M")
        self.assertEqual(validators.validate_company("شركة النور"), "شركة النور")
        for junk in ("", "x", "n/a", "NA", "test", "asdf", "----", "12345", "none"):
            self.assertEqual(_code(validators.validate_company, junk), "invalid", junk)

    def test_person(self):
        self.assertEqual(validators.validate_person_name(" Jean-Luc ", "first name"), "Jean-Luc")
        for junk in ("", "123", "http://spam.example", "<b>x</b>", "a@b.com"):
            self.assertEqual(_code(validators.validate_person_name, junk, "first name"), "invalid", junk)


class CatalogTests(SimpleTestCase):
    def test_default_catalogue_is_valid(self):
        release = catalog.current_release()
        self.assertEqual([a["key"] for a in release["artifacts"]], ["vmware", "hyperv"])
        self.assertEqual(catalog.human_size(1721163264), "1.60 GiB")

    def test_unsafe_catalogue_is_refused(self):
        def variant(**change):
            artifact = dict(catalog.DEFAULT_CATALOG["releases"]["1.25.2"]["artifacts"][0], **change)
            released = change.pop("released", "2026-11-07")
            artifact = {k: v for k, v in artifact.items() if k != "released"}
            return {"current": "1.25.2", "releases": {"1.25.2": {
                "version": "1.25.2", "released": released, "artifacts": [artifact]}}}

        for change in ({"filename": "../../etc/passwd"}, {"filename": "a/b.ova"}, {"filename": ".hidden"},
                       {"sha256": "abc"}, {"key": "vm ware"}, {"key": ""}, {"released": "7 Nov"}):
            with self.assertRaises(catalog.CatalogError, msg=change):
                catalog._check(variant(**change))
        with self.assertRaises(catalog.CatalogError):
            catalog._check({"current": "9.9.9", "releases": {}})
