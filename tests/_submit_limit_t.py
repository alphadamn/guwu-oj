import sys
import os
sys.path.insert(0, '/www/wwwroot/guwu-oj')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oj_project.settings')
import django
django.setup()

from django.test import Client
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from problems.models import Problem
from submissions.models import Submission

User = get_user_model()

problem = Problem.objects.filter(is_public=True).order_by('id').first()
if not problem:
    problem = Problem.objects.create(
        title='test-submit-enforcement',
        description='for testing',
        time_limit=1000,
        memory_limit=256,
        is_public=True,
        difficulty='简单',
    )

user, _ = User.objects.get_or_create(
    username='__test_feature_submit',
    defaults={'email': 'f@example.com'},
)
user.set_password('pw123pw123')
user.is_active = True
user.is_permanently_banned = False
user.banned_until = None
user.disabled_features = ''
user.disabled_features_until = None
user.save()

Submission.objects.filter(user=user).delete()

c = Client()
c.post('/users/login/', {
    'username': user.username,
    'password': 'pw123pw123',
})

print('Step 1 (clean user):')
resp = c.post(f'/submissions/submit/{problem.id}/', {
    'language': 'Python', 'code': 'print("hi")',
})
print('  status:', resp.status_code)
print('  created:', Submission.objects.filter(user=user).count())

print('Step 2 (disabled_features=submit, no deadline):')
user.disabled_features = 'submit'
user.disabled_features_until = None
user.save()
Submission.objects.filter(user=user).delete()
resp = c.post(f'/submissions/submit/{problem.id}/', {
    'language': 'Python', 'code': 'print("hi2")',
})
print('  status:', resp.status_code)
made = Submission.objects.filter(user=user).count()
print('  created (should be 0):', made)
body = resp.content.decode('utf-8', 'ignore')
print('  has disabled notice:', '提交功能已被管理员禁用' in body)

print('Step 3 (disabled_features with future deadline):')
user.disabled_features_until = timezone.now() + timedelta(days=3)
user.save()
resp = c.post(f'/submissions/submit/{problem.id}/', {
    'language': 'Python', 'code': 'print("hi3")',
})
print('  status:', resp.status_code)
print('  created (should be 0):', Submission.objects.filter(user=user).count())

print('Step 4 (disabled_features with past deadline):')
user.disabled_features_until = timezone.now() - timedelta(days=1)
user.save()
resp = c.post(f'/submissions/submit/{problem.id}/', {
    'language': 'Python', 'code': 'print("hi4")',
})
print('  status:', resp.status_code)
print('  created:', Submission.objects.filter(user=user).count())

print('DONE')
