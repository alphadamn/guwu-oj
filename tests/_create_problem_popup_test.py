import sys
import os
sys.path.insert(0, '/www/wwwroot/guwu-oj')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oj_project.settings')
import django
django.setup()

from django.test import Client
from django.contrib.auth import get_user_model
import re

User = get_user_model()
u, _ = User.objects.get_or_create(username='__test_popup')
u.set_password('pw123pw123')
u.is_active = True
u.disabled_features = 'create_problem'
u.disabled_features_until = None
u.save()

c = Client()
c.post('/users/login/', {'username': u.username, 'password': 'pw123pw123'})

# Step 1: visit /problems/create/ → 302 to /
resp1 = c.get('/problems/create/')
print('A) create_problem redirect?', resp1.status_code, resp1.get('Location'))
assert resp1.status_code == 302, 'expected redirect'

# Step 2: visit / → check session payload and rendered JSON.parse
resp2 = c.get('/')
print('B) home status:', resp2.status_code)
body = resp2.content.decode('utf-8', 'ignore')
m = re.search(r"pagePunishment = JSON\.parse\('([^']*)'\)", body)
print('C) JSON.parse present?', bool(m))
if m:
    print('   first 60 chars of JSON:', m.group(1)[:100])
print('D) header kind-feature_ban present?', 'kind-feature_ban' in body)
print('E) "上传题目功能已被禁用" in body?', '上传题目功能已被禁用' in body)

# Restore
u.disabled_features = ''
u.save()
print('DONE')
