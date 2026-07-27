import sys, os
sys.path.insert(0, "/www/wwwroot/guwu-oj")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "oj_project.settings")
import django
django.setup()
from django.test import Client
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.cache import cache
from datetime import timedelta
import re
import json
import codecs

User = get_user_model()
u = User.objects.get(username='__test_popup')
u.set_password('pw123pw123')
u.is_active = True
u.save()


def parse_pagePunishment(body):
    for line in body.splitlines():
        if 'JSON.parse' in line and 'pagePunishment' in line:
            try:
                s = line.split("JSON.parse('", 1)[1].split("')", 1)[0]
                decoded = codecs.decode(s, 'unicode_escape')
                return json.loads(decoded)
            except Exception:
                pass
    return None


def has_hidden_next(body, value):
    pat = 'name="next"[^>]*value="' + value + '"'
    return bool(re.search(pat, body))


def do_login(c, username, next_url=None):
    url = '/users/login/' + ('?next=' + next_url if next_url else '')
    c.get(url)
    resp_img = c.get('/users/captcha/image/')
    cid = resp_img.headers.get('X-Captcha-Id', '') or ''
    ans = cache.get('captcha:challenge:' + cid, '') or ''
    data = {
        'username': username, 'password': 'pw123pw123',
        'captcha_id': cid, 'captcha_answer': ans,
    }
    if next_url:
        data['next'] = next_url
    return c.post('/users/login/', data)


# A
u.is_permanently_banned = True
u.banned_reason = 'A reason'
u.banned_until = None
u.disabled_features = ''
u.save()
c = Client()
resp = do_login(c, u.username, next_url='/submissions/my/')
body = resp.content.decode('utf-8', 'ignore')
obj = parse_pagePunishment(body)
print('A: status=', resp.status_code, 'kind=', obj.get('kind') if obj else None)
print('A: hidden_next=', has_hidden_next(body, '/submissions/my/'), 'authed=', bool(c.session.get('_auth_user_id')))

# B: temp ban
u.is_permanently_banned = False
u.banned_until = timezone.now() + timedelta(days=5)
u.banned_reason = 'B reason'
u.save()
c = Client()
resp = do_login(c, u.username, next_url='/problems/')
body = resp.content.decode('utf-8', 'ignore')
obj = parse_pagePunishment(body)
print('B: status=', resp.status_code, 'kind=', obj.get('kind') if obj else None)
print('B: hidden_next=', has_hidden_next(body, '/problems/'))

# C: mid-session ban
u.is_permanently_banned = False
u.banned_until = None
u.save()
c = Client()
do_login(c, u.username)
print('C: pre-ban authed:', bool(c.session.get('_auth_user_id')))
u.is_permanently_banned = True
u.banned_reason = 'mid-session ban'
u.save()
resp3 = c.get('/')
print('C: get/ status:', resp3.status_code, 'Location:', resp3.get('Location'))
resp4 = c.get('/users/login/')
body4 = resp4.content.decode('utf-8', 'ignore')
obj4 = parse_pagePunishment(body4)
print('C: modal kind:', obj4.get('kind') if obj4 else None, 'reason:', obj4.get('reason') if obj4 else None)
print('C: authed after:', bool(c.session.get('_auth_user_id')))

# D: feature ban
u.is_permanently_banned = False
u.banned_until = None
u.disabled_features = 'submit,comment'
u.disabled_features_until = timezone.now() + timedelta(days=7)
u.save()
c = Client()
resp = do_login(c, u.username, next_url='/submissions/my/')
print('D: status=', resp.status_code, 'authed=', bool(c.session.get('_auth_user_id')))
resp2 = c.get('/')
body2 = resp2.content.decode('utf-8', 'ignore')
obj2 = parse_pagePunishment(body2)
print('D: home modal kind=', obj2.get('kind') if obj2 else None)

# E: clean user - no modal
u.is_permanently_banned = False
u.banned_until = None
u.disabled_features = ''
u.disabled_features_until = None
u.save()
c = Client()
resp = do_login(c, u.username, next_url='/submissions/my/')
body = resp.content.decode('utf-8', 'ignore')
print('E: status=', resp.status_code, 'Location=', resp.get('Location'), 'json_snippet_present=', bool(re.search(r'pagePunishment = JSON\.parse', body)))

print('ALL_OK')
