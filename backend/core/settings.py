"""
Django settings for HOS Route Planner (core) project.

Built for the FMCSA Hours-of-Service trip planner assessment:
- DRF API for geocoding, routing and duty-cycle schedule generation
- WhiteNoise for static files (Vercel / any PaaS friendly)
- SQLite by default, Postgres via DATABASE_URL when available
"""
import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me-in-production"
)
DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() in ("1", "true", "yes")

ALLOWED_HOSTS = ["*"]  # tighten in production via env if desired

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "corsheaders",
    "apps.routing",
    "apps.trips",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=600
    )
}

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "UNAUTHENTICATED_USER": None,
}

# CORS: allow the React front-end (Vercel preview/prod + localhost dev)
CORS_ALLOW_ALL_ORIGINS = True

# Static files (WhiteNoise)
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"

# ---------------------------------------------------------------------------
# Routing / external free APIs (no key required)
# ---------------------------------------------------------------------------
ROUTING_CONFIG = {
    "OSRM_BASE_URL": os.environ.get(
        "OSRM_BASE_URL", "https://router.project-osrm.org"
    ),
    "NOMINATIM_URL": os.environ.get(
        "NOMINATIM_URL", "https://nominatim.openstreetmap.org/search"
    ),
    "HTTP_TIMEOUT": 20,
    "USER_AGENT": "hos-route-planner/1.0 (full-stack assessment)",
    # Average highway speed used only as a fallback when the router is down.
    "FALLBACK_SPEED_MPH": float(os.environ.get("FALLBACK_SPEED_MPH", "52")),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
