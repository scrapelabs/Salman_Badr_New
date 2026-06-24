from django.urls import path

from . import views

urlpatterns = [
    path("", views.login_view, name="login"),
    path("dashboard/", views.dashboard_view, name="dashboard"),
    path("scraper-directory/", views.scraper_directory_view, name="scraper_directory"),
    path("run-history/", views.run_history_view, name="run_history"),
    path("logout/", views.logout_view, name="logout"),
]
