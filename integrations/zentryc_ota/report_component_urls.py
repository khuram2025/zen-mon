"""Include this URLconf under /api/v1/ in the Zentryc OTA application."""
from django.urls import path
from rest_framework.decorators import api_view

from .report_component import download

urlpatterns = [path("appliances/components/os-patch-reporter", api_view(["GET"])(download))]
