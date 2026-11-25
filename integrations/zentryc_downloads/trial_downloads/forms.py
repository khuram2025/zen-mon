import time

from django import forms
from django.core import signing

from . import conf, validators
from .countries import COUNTRIES
from .models import DEVICE_BANDS, HYPERVISORS

_STAMP_SALT = "trial-downloads.form-opened"


def new_stamp():
    """Signed time the form was shown, to tell a person from a script."""
    return signing.dumps(int(time.time()), salt=_STAMP_SALT)


def _text(autocomplete, **attrs):
    attrs["autocomplete"] = autocomplete
    return forms.TextInput(attrs=attrs)


class TrialRequestForm(forms.Form):
    first_name = forms.CharField(label="First name", max_length=100, widget=_text("given-name"))
    last_name = forms.CharField(label="Last name", max_length=100, widget=_text("family-name"))
    email = forms.CharField(
        label="Work email",
        max_length=254,
        help_text="We send the download link to this address.",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "name@company.com"}),
    )
    company = forms.CharField(label="Company", max_length=200, widget=_text("organization"))
    job_title = forms.CharField(label="Job title", max_length=120, widget=_text("organization-title"))
    country = forms.ChoiceField(
        label="Country",
        choices=(("", "Select a country"),) + COUNTRIES,
        widget=forms.Select(attrs={"autocomplete": "country"}),
    )
    phone = forms.CharField(
        label="Phone",
        max_length=40,
        help_text="With the country code.",
        widget=forms.TextInput(attrs={"autocomplete": "tel", "inputmode": "tel", "placeholder": "+966 50 123 4567"}),
    )
    device_count = forms.ChoiceField(
        label="Devices and servers to monitor", choices=(("", "Select a range"),) + DEVICE_BANDS
    )
    hypervisor = forms.ChoiceField(label="Where will you run it?", choices=(("", "Select a platform"),) + HYPERVISORS)
    current_tool = forms.CharField(
        label="Current monitoring tool",
        max_length=80,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    accept_terms = forms.BooleanField(
        required=True,
        error_messages={"required": "Accept the terms to download the evaluation software."},
    )
    marketing_opt_in = forms.BooleanField(required=False)

    # Not shown to people: a filled "website" or a form posted within seconds
    # is treated as automated (see views.trial_request).
    website = forms.CharField(required=False, widget=forms.HiddenInput)
    opened = forms.CharField(required=False, widget=forms.HiddenInput)
    src = forms.CharField(required=False, max_length=300, widget=forms.HiddenInput)

    error_messages = {"required": "This field is required."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if field.required and name != "accept_terms":
                field.widget.attrs["required"] = True
                field.error_messages["required"] = "Enter your %s." % field.label.lower()
        for name in ("country", "device_count", "hypervisor"):
            self.fields[name].error_messages["required"] = "Select an option."

    def clean_first_name(self):
        return validators.validate_person_name(self.cleaned_data["first_name"], "first name")

    def clean_last_name(self):
        return validators.validate_person_name(self.cleaned_data["last_name"], "last name")

    def clean_email(self):
        return validators.validate_work_email(self.cleaned_data["email"]).lower()

    def clean_company(self):
        return validators.validate_company(self.cleaned_data["company"])

    def clean_job_title(self):
        return validators.validate_person_name(self.cleaned_data["job_title"], "job title")

    def clean_country(self):
        country = self.cleaned_data["country"]
        if country in set(conf.get("BLOCKED_COUNTRIES")):
            raise forms.ValidationError(
                "We cannot offer this download in your country. Contact us if you have questions.",
                code="blocked_country",
            )
        return country

    def clean_current_tool(self):
        return " ".join(self.cleaned_data.get("current_tool", "").split())

    def clean(self):
        cleaned = super().clean()
        # The phone is read with the chosen country, so a national number
        # (050 123 4567) is accepted once the country is known.
        if "phone" in cleaned:
            try:
                cleaned["phone"] = validators.normalize_phone(cleaned["phone"], cleaned.get("country"))
            except forms.ValidationError as error:
                self.add_error("phone", error)
        return cleaned

    def looks_automated(self):
        """Call on bound data, valid or not."""
        if (self.data.get("website") or "").strip():
            return True
        try:
            opened = signing.loads(self.data.get("opened") or "", salt=_STAMP_SALT, max_age=24 * 3600)
        except signing.BadSignature:
            return True
        return time.time() - opened < conf.get("MIN_FILL_SECONDS")
