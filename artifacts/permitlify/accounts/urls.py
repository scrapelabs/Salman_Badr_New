from django.urls import path

from . import qa_views, views

urlpatterns = [
    path("", views.login_view, name="login"),
    path("overview/", views.overview_view, name="overview"),
    path("stats/live/", views.live_stats_view, name="live_stats"),
    path("scrapers/", views.scrapers_view, name="scrapers"),
    path("scrapers/<slug:slug>/", views.scraper_detail_view, name="scraper_detail"),
    path("scrapers/<slug:slug>/run/", views.scraper_run_view, name="scraper_run"),
    path("scrapers/<slug:slug>/queue/", views.scraper_queue_view, name="scraper_queue"),
    path(
        "scrapers/<slug:slug>/queue/events/",
        views.queue_events_view,
        name="queue_events",
    ),
    path(
        "scrapers/<slug:slug>/start-status/",
        views.scraper_start_status_view,
        name="scraper_start_status",
    ),
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
        "scrapers/<slug:slug>/runs/<uuid:run_uuid>/cancel/",
        views.run_cancel_view,
        name="run_cancel",
    ),
    path(
        "scrapers/<slug:slug>/matches.csv",
        views.college_matches_export_view,
        name="college_matches_export",
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
    # QA Team Tasks
    path("qa/", qa_views.board, name="qa_board"),
    path("qa/new/", qa_views.ticket_create, name="qa_ticket_create"),
    path("qa/t/<uuid:uuid>/", qa_views.ticket_detail, name="qa_ticket"),
    path("qa/t/<uuid:uuid>/update/", qa_views.ticket_update, name="qa_ticket_update"),
    path("qa/t/<uuid:uuid>/edit/", qa_views.ticket_edit, name="qa_ticket_edit"),
    path("qa/t/<uuid:uuid>/delete/", qa_views.ticket_delete, name="qa_ticket_delete"),
    path("qa/t/<uuid:uuid>/comment/", qa_views.comment_add, name="qa_comment_add"),
    path("qa/mention-users/", qa_views.mention_users, name="qa_mention_users"),
    path("qa/attachments/", qa_views.attachment_upload, name="qa_attachment_upload"),
    path(
        "qa/attachments/<uuid:uuid>/",
        qa_views.attachment_serve,
        name="qa_attachment",
    ),
    path(
        "qa/notifications/poll/",
        qa_views.notifications_poll,
        name="qa_notifications_poll",
    ),
    path(
        "qa/notifications/read-all/",
        qa_views.notifications_read_all,
        name="qa_notifications_read_all",
    ),
    path(
        "qa/notifications/<int:pk>/open/",
        qa_views.notification_open,
        name="qa_notification_open",
    ),
    path("logout/", views.logout_view, name="logout"),
]
