from django.urls import path

from . import views

urlpatterns = [
    path("", views.login_view, name="login"),
    path("overview/", views.overview_view, name="overview"),
    path("scrapers/", views.scrapers_view, name="scrapers"),
    path("scrapers/<slug:slug>/", views.scraper_detail_view, name="scraper_detail"),
    path("scrapers/<slug:slug>/run/", views.scraper_run_view, name="scraper_run"),
    path(
        "scrapers/<slug:slug>/runs/<uuid:run_uuid>/log/",
        views.run_log_view,
        name="run_log",
    ),
    path(
        "scrapers/<slug:slug>/runs/<uuid:run_uuid>/log.txt",
        views.run_log_download_view,
        name="run_log_download",
    ),
    path(
        "scrapers/<slug:slug>/runs/<uuid:run_uuid>/data.csv",
        views.run_csv_download_view,
        name="run_csv_download",
    ),
    path(
        "scrapers/<slug:slug>/runs/<uuid:run_uuid>/requests.csv",
        views.run_requests_download_view,
        name="run_requests_download",
    ),
    path(
        "scrapers/<slug:slug>/runs/<uuid:run_uuid>/errors.csv",
        views.run_errors_download_view,
        name="run_errors_download",
    ),
    path(
        "scrapers/<slug:slug>/runs/<uuid:run_uuid>/delete/",
        views.run_delete_view,
        name="run_delete",
    ),
    path(
        "scrapers/<slug:slug>/runs/bulk-delete/",
        views.runs_bulk_delete_view,
        name="runs_bulk_delete",
    ),
    path(
        "scrapers/<slug:slug>/runs/<uuid:run_uuid>/events/",
        views.run_events_view,
        name="run_events",
    ),
    path(
        "scrapers/<slug:slug>/runs/<uuid:run_uuid>/stop/",
        views.stop_run_view,
        name="run_stop",
    ),
    path(
        "scrapers/<slug:slug>/trigger/",
        views.scraper_trigger_view,
        name="scraper_trigger",
    ),
    path("proxies/", views.proxies_view, name="proxies"),
    path("apis/", views.apis_view, name="apis"),
    path("apis/logs/", views.apis_logs_view, name="apis_logs"),
    path("requirements/", views.requirements_view, name="requirements"),
    path("companies/", views.companies_view, name="companies"),
    path("settings/", views.settings_view, name="settings"),
    path("users/", views.users_view, name="users"),
    path("logout/", views.logout_view, name="logout"),
]
