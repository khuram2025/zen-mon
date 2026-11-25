"""Settings for running the trial_downloads tests outside the website. Never deployed."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRET_KEY = "tests-only-not-a-secret"
DEBUG = False
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "testproject.urls"
USE_TZ = True
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "trial_downloads",
]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [p for p in (os.environ.get("TRIAL_PREVIEW_TEMPLATES"), os.path.join(BASE_DIR, "templates")) if p],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": os.environ.get("TRIAL_PREVIEW_DB", ":memory:")}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
EMAIL_BACKEND = os.environ.get("TRIAL_PREVIEW_EMAIL", "django.core.mail.backends.locmem.EmailBackend")
EMAIL_FILE_PATH = os.environ.get("TRIAL_PREVIEW_MAILDIR", "")
DEFAULT_FROM_EMAIL = "Zentryc <downloads@zentryc.example>"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
TRIAL_DOWNLOADS = {
    "SITE_URL": os.environ.get("TRIAL_PREVIEW_SITE", "https://zentryc.example"),
    "CHECK_MX": False,
    "NOTIFY": ["sales@zentryc.example"],
}
