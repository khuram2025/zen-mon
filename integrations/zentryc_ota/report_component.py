"""Zentryc's separately authenticated download for the qualified OS reporter."""
from hashlib import sha256
from pathlib import Path

FIRMWARE = "1.25.1"
FILENAME = "zenplus-os-patch-reporter-v1.0.0.zup"
DIGEST = "6e22f0692bb0d5c75f19e99422a1250ca4b30ab62532854a990ec366a462e44c"
MAX_BYTES = 256 * 1024


def eligible(appliance, requested_firmware, reference_id):
    # The pilot is a single exact enrollment. A missing setting disables it.
    subscription = appliance.subscription
    return bool(reference_id and str(appliance.id) == str(reference_id)
                and appliance.is_active and appliance.arch == "amd64"
                and subscription and subscription.subscription_type == "ota"
                and subscription.is_active and not subscription.is_expired
                and requested_firmware == appliance.current_version == FIRMWARE)


def read_artifact(directory):
    with (Path(directory) / FILENAME).open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES or sha256(data).hexdigest() != DIGEST:
        raise ValueError("Unqualified component artifact")
    return data


def download(request):
    from django.conf import settings
    from django.http import HttpResponse
    from ota.auth import authenticate_appliance

    appliance = authenticate_appliance(request)
    selected = getattr(settings, "OS_REPORT_REFERENCE_ID", None)
    if not eligible(appliance, request.headers.get("X-ZenPlus-Firmware"), selected):
        response = HttpResponse(status=204)
    else:
        try:
            data = read_artifact(settings.OS_REPORT_PRIVATE_DIRECTORY)
        except (AttributeError, OSError, ValueError):
            response = HttpResponse(status=503)
        else:
            response = HttpResponse(data, content_type="application/octet-stream")
            response["Content-Disposition"] = f'attachment; filename="{FILENAME}"'
    response["Cache-Control"] = "private, no-store"
    return response
