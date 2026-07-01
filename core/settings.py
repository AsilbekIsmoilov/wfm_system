from pathlib import Path
import os
BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "SECRET_KEY",
    'django-insecure-b1*pfptn-z#hp)vxl27_ok+vi_5b5f4y1-cnzc@7c6eafr%v54',
)

DEBUG = os.environ.get("DEBUG", "1") == "1"


ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")

CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CSRF_TRUSTED_ORIGINS",
        "http://10.145.20.9:4020",
    ).split(",")
    if o.strip()
]

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'hourly_locks',
    'archive',
    'attendance',
    'rest_framework',
    "django_filters",
    "drf_spectacular",
    "drf_spectacular_sidecar",
    "corsheaders",
    "channels",
]


CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [f"{REDIS_URL}/5"],
        },
    },
}

ASGI_APPLICATION = "core.asgi.application"

REST_FRAMEWORK = {
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # JWT первым: при наличии Bearer-токена он выигрывает и проверка CSRF
        # не выполняется. SessionAuthentication (browsable API / admin) —
        # вторым: срабатывает только когда Bearer-токена нет.
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Отработка API",
    "DESCRIPTION": "Contact Center LTD",
    "VERSION": "1.0.0",
}

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'

CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_ALL_ORIGINS = True

from datetime import timedelta
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=2),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "ALGORITHM": "HS256",
    "UPDATE_LAST_LOGIN": True,
}


# https://docs.djangoproject.com/en/5.2/ref/settings/#databases
_DB_USER = os.environ.get("DB_USER", "root")
_DB_PASSWORD = os.environ.get("DB_PASSWORD", "password123")
_DB_HOST = os.environ.get("DB_HOST", "localhost")
_DB_PORT = os.environ.get("DB_PORT", "3306")

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.environ.get("DB_NAME", "project1"),
        'USER': _DB_USER,
        'PASSWORD': _DB_PASSWORD,
        'HOST': _DB_HOST,
        'PORT': _DB_PORT,
        'CONN_MAX_AGE': 60,
    },
    "archive": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ.get("DB_NAME_ARCHIVE", "project1_archive"),
        "USER": _DB_USER,
        "PASSWORD": _DB_PASSWORD,
        "HOST": _DB_HOST,
        "PORT": _DB_PORT,
        'CONN_MAX_AGE': 60,
    },
}


DATABASE_ROUTERS = [
    "core.db_router.ArchiveRouter",
]

# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Asia/Tashkent'

USE_I18N = True

USE_TZ = True




STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'


DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = "hourly_locks.User"


CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"{REDIS_URL}/1",
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "IGNORE_EXCEPTIONS": True,
        },
        "TIMEOUT": 1200,
    }
}

LOG_DIR = Path(BASE_DIR) / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,

    "formatters": {
        "verbose": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        },
    },

    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },

        "file_func_runner": {
            "level": "INFO",
            "class": "logging.FileHandler",
            "filename": LOG_DIR / "func_runner.log",
            "formatter": "verbose",
            "encoding": "utf-8",
        },
    },

    "loggers": {
        "django.cache": {
            "handlers": ["console"],
            "level": "DEBUG",
        },

        "func_runner": {
            "handlers": ["file_func_runner", "console"],
            "level": "INFO",
            "propagate": False,
        },


        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
}


CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", f"{REDIS_URL}/3")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", f"{REDIS_URL}/4")
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_TIMEZONE = 'Asia/Tashkent'



GOOGLE_CREDENTIAL_PATH = os.environ.get(
    "GOOGLE_CREDENTIAL_PATH",
    str(BASE_DIR / "creds.json"),
)

WFM_BASE_URL = os.environ.get("WFM_BASE_URL", "http://localhost:3000/api/v1")

SYNC_SERVICE_TOKEN = os.environ.get(
    "SYNC_SERVICE_TOKEN",
    "e31e9b0a9f9b4bffb92240e6f7b70df3255763cd18dc447aa68992cffc1cf949",
)

WFM_HTTP_TIMEOUT = int(os.environ.get("WFM_HTTP_TIMEOUT", "30"))
