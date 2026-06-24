from django.urls import path

from . import views

urlpatterns = [
    path("", views.login_view, name="login"),
    path("overview/", views.overview_view, name="overview"),
    path("scrapers/", views.scrapers_view, name="scrapers"),
    path("scrapers/<slug:slug>/", views.scraper_detail_view, name="scraper_detail"),
    path("schedule/", views.schedule_view, name="schedule"),
    path("proxies/", views.proxies_view, name="proxies"),
    path("apis/", views.apis_view, name="apis"),
    path("apis/logs/", views.apis_logs_view, name="apis_logs"),
    path("requirements/", views.requirements_view, name="requirements"),
    path("companies/", views.companies_view, name="companies"),
    path("settings/", views.settings_view, name="settings"),
    path("users/", views.users_view, name="users"),
    path("logout/", views.logout_view, name="logout"),
]
