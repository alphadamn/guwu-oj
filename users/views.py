from django.http import HttpResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import reverse_lazy
from django.contrib import messages
from django.utils.cache import patch_response_headers
from django.utils.http import http_date
from django_ratelimit.decorators import ratelimit
from django.core.cache import cache
import time

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
@ratelimit(key='user', rate='3/m', method='POST')
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
    """Serve a user's avatar directly from the PostgreSQL database with Redis caching."""
    user = get_object_or_404(User, username=username)
    if not user.has_avatar:
        raise Http404('No avatar stored for this user')

    blob = user.avatar_blob
    
    # Cache keys
    cache_key = f'avatar_data:{username}'
    freq_key = f'avatar_freq:{username}'
    
    # Track request frequency using a simple counter with 1-minute expiration
    # Increment counter for this minute
    current_minute = int(time.time() // 60)
    minute_key = f'{freq_key}:{current_minute}'
    
    # Use get_or_set to ensure key exists before incrementing
    request_count = cache.get_or_set(minute_key, 0, timeout=60)
    request_count = cache.incr(minute_key)
    cache.expire(minute_key, 60)
    
    # Check if frequency >= 5/min
    should_cache = request_count >= 5
    
    # Try to get from cache first
    cached_data = cache.get(cache_key)
    if cached_data:
        cached_content_type, cached_image_data, cached_updated_at = cached_data
        # Verify cache is still valid (avatar hasn't been updated)
        if cached_updated_at == blob.updated_at.timestamp():
            response = HttpResponse(cached_image_data, content_type=cached_content_type)
            response['Last-Modified'] = http_date(blob.updated_at.timestamp())
            patch_response_headers(response, cache_timeout=0)
            response['X-Cache'] = 'HIT'
            return response
    
    # Fetch from database
    image_data = blob.image_data
    content_type = blob.content_type
    updated_at = blob.updated_at.timestamp()
    
    # Cache if frequency threshold reached
    if should_cache:
        cache.set(cache_key, (content_type, image_data, updated_at), timeout=3600)
    
    response = HttpResponse(image_data, content_type=content_type)
    response['Last-Modified'] = http_date(blob.updated_at.timestamp())
    patch_response_headers(response, cache_timeout=0)
    response['X-Cache'] = 'MISS'
    return response
