from django.http import HttpResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import reverse_lazy
from django.contrib import messages
from django.utils.cache import patch_response_headers
from django.utils.http import http_date

from .forms import UserRegisterForm, UserUpdateForm
from .models import User


class CustomLoginView(LoginView):
    template_name = 'users/login.html'
    redirect_authenticated_user = True


class CustomLogoutView(LogoutView):
    next_page = reverse_lazy('home')


def register(request):
    if request.method == 'POST':
        form = UserRegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, '注册成功！')
            return redirect('home')
    else:
        form = UserRegisterForm()
    return render(request, 'users/register.html', {'form': form})


@login_required
def profile(request, username):
    user = get_object_or_404(User, username=username)
    return render(request, 'users/profile.html', {'user_profile': user})


@login_required
def edit_profile(request):
    if request.method == 'POST':
        files = request.FILES if 'avatar' in request.FILES else None
        post_data = request.POST.copy()
        if 'avatar' not in request.FILES:
            post_data.pop('avatar', None)
        form = UserUpdateForm(post_data, files, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, '个人资料更新成功！')
            return redirect('profile', username=request.user.username)
    else:
        form = UserUpdateForm(instance=request.user)
    return render(request, 'users/edit_profile.html', {'form': form})


@login_required
def avatar(request, username):
    """Serve a user's avatar directly from the PostgreSQL database."""
    user = get_object_or_404(User, username=username)
    if not user.has_avatar:
        raise Http404('No avatar stored for this user')

    blob = user.avatar_blob
    response = HttpResponse(blob.image_data, content_type=blob.content_type)
    # Always revalidate; a query-string version bump is added to the URL on
    # upload to force clients to fetch the new image.
    response['Last-Modified'] = http_date(blob.updated_at.timestamp())
    patch_response_headers(response, cache_timeout=0)
    return response
