import sys
import os
sys.path.insert(0, '/www/wwwroot/guwu-oj')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oj_project.settings')
import django
django.setup()

from django.test import Client
from django.contrib.auth import get_user_model

User = get_user_model()

u, _ = User.objects.get_or_create(
    username='__test_create_problem',
    defaults={'email': 'cp@example.com'},
)
u.set_password('pw123pw123')
u.is_active = True
u.is_permanently_banned = False
u.banned_until = None
u.disabled_features = ''
u.disabled_features_until = None
u.save()

def login(c):
    c.post('/users/login/', {
        'username': u.username, 'password': 'pw123pw123',
    })

# 1) clean user: should succeed (form valid/invalid doesn't matter as long as we get HTML)
c = Client()
login(c)
resp = c.get('/problems/create/')
print('1) clean user GET /problems/create/ ->', resp.status_code)
body = resp.content.decode('utf-8', 'ignore')
print('   "new problem" form present?', 'test_cases' in body or '标题' in body or 'time_limit' in body.lower())

# 2) disabled: create_problem.
u.disabled_features = 'create_problem'
u.disabled_features_until = None
u.save()
resp = c.get('/problems/create/')
print('2) create_problem disabled: GET ->', resp.status_code, 'location:', resp.get('Location'))
print('   redirected to home?', (resp.get('Location') or '').endswith('/'))

# 3) feature-specific check via template tag: / (home) should no longer contain the active upload link.
resp2 = c.get('/')
body2 = resp2.content.decode('utf-8', 'ignore')
print('3) home page still has link /problems/create/?',
      '/problems/create/"' in body2 or '/problems/create/\'' in body2)

# Clean up ban state for future tests.
u.disabled_features = ''
u.save()
print('DONE')
