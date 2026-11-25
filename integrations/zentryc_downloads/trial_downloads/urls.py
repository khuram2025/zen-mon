"""Include before the site's own URL configuration, so that ``/downloads/``
resolves here:

    path("", include("trial_downloads.urls")),
"""
from django.urls import path, re_path

from . import views

app_name = "trial_downloads"

urlpatterns = [
    path("downloads/", views.downloads_index, name="index"),
    path("downloads/trial/", views.trial_request, name="request"),
    path("downloads/trial/sent/", views.trial_sent, name="sent"),
    re_path(r"^downloads/trial/d/(?P<token>[A-Za-z0-9_:.\-]{20,400})/$", views.trial_access, name="access"),
    re_path(
        r"^downloads/trial/d/(?P<token>[A-Za-z0-9_:.\-]{20,400})/(?P<key>[a-z0-9]{1,40})/$",
        views.trial_file,
        name="file",
    ),
    path("downloads/zenplus-appliance/", views.release_latest, name="release_latest"),
    re_path(r"^downloads/zenplus-appliance/(?P<version>[0-9]+\.[0-9]+\.[0-9]+)/$", views.release_page, name="release"),
]
