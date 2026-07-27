import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "oj_project.settings")
import django; django.setup()
from django.test import Client
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta

User = get_user_model()

user, _ = User.objects.get_or_create(
    username='__test_popup',
    defaults={'email': 'tp@example.com', 'is_active': True},
)
user.set_password('pw123pw123')
user.is_active = True
user.save()

from django.core.cache import cache

def do_login(c, username):
    c.get('/users/login/')
    resp_img = c.get('/users/captcha/image/')
    cid = resp_img.headers.get('X-Captcha-Id', '')
    ans = cache.get('captcha:challenge:' + cid, '') or ''
    return c.post('/users/login/', {
        'username': username, 'password': 'pw123pw123',
        'captcha_id': cid, 'captcha_answer': ans,
    })

# 1 — permanent ban: page should carry real JSON object for the shared modal
user.is_permanently_banned = True
user.banned_reason = '测试永久封号弹窗'
user.save()
c = Client()
resp = do_login(c, user.username)
body = resp.content.decode('utf-8', 'ignore')
print('PERM BAN status:', resp.status_code, 'authed?', bool(c.session.get('_auth_user_id')))
m = re.search(r'pagePunishment = JSON\.parse\(\'(.*?)\'\)', body)
if m:
    print('  JSON snippet:', m.group(1)[:120])
else:
    m2 = re.search(r'"kind"\s*:\s*"permanent_ban"', body)
    print('  embedded permanent_ban in HTML?', bool(m2))
    # Also print surrounding lines for debugging.
    for i, line in enumerate(body.splitlines()):
        if 'pagePunishment' in line or 'punishment_json' in line or 'punishment' in line and ('JSON' in line or 'kind' in line):
            print('  LINE', i, ':', line.strip()[:200])

# 2 — temp ban
user.is_permanently_banned = False
user.banned_until = timezone.now() + timedelta(days=5)
user.banned_reason = '辱骂其他用户'
user.save()
c = Client()
resp = do_login(c, user.username)
body = resp.content.decode('utf-8', 'ignore')
print('TEMP BAN status:', resp.status_code, 'authed?', bool(c.session.get('_auth_user_id')))
print('  embedded kind?', bool(re.search(r'"kind"\s*:\s*"temporary_ban"', body)))

# 3 — feature ban
user.banned_until = None
user.disabled_features = 'submit,comment'
user.disabled_features_until = timezone.now() + timedelta(days=7)
user.save()
c = Client()
resp = do_login(c, user.username)
body = resp.content.decode('utf-8', 'ignore')
print('FEATURE BAN status:', resp.status_code, 'authed?', bool(c.session.get('_auth_user_id')))
print('  session.notice present?', bool(c.session.get('punishment_notice')))
# Now do a GET / so base.html is responsible for the popup.
resp2 = c.get('/')
body2 = resp2.content.decode('utf-8', 'ignore')
print('  next GET / status:', resp2.status_code)
print('  next page has kind=feature_ban?', bool(re.search(r'"kind"\s*:\s*"feature_ban"', body2)))

# 4 — clean user: no popup
user.disabled_features = ''
user.disabled_features_until = None
user.banned_reason = ''
user.save()
c = Client()
resp = do_login(c, user.username)
body = resp.content.decode('utf-8', 'ignore')
print('CLEAN status:', resp.status_code, 'has any kind in HTML?', bool(re.search(r'"kind"\s*:\s*"', body)))

# 5 — AJAX clear endpoint
c = Client()
do_login(c, user.username)
resp = c.post('/users/clear-punishment-notice/')
print('AJAX clear-notice status:', resp.status_code)
print('ALL OK')
