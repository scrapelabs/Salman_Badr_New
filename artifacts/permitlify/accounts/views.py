from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    error = None
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect("dashboard")
        error = "Invalid username or password."

    return render(request, "login.html", {"error": error})


@login_required
def dashboard_view(request):
    return render(request, "dashboard.html")


@require_http_methods(["POST"])
def logout_view(request):
    logout(request)
    return redirect("login")
