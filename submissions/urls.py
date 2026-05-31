from django.urls import path
from .views import (
    all_submissions,
    submission_detail,
    submission_list,
    submission_status_api,
    submit_solution,
)

urlpatterns = [
    path('submit/<int:problem_id>/', submit_solution, name='submit_solution'),
    path('api/<int:submission_id>/status/', submission_status_api, name='submission_status_api'),
    path('detail/<int:submission_id>/', submission_detail, name='submission_detail'),
    path('my/', submission_list, name='my_submissions'),
    path('all/', all_submissions, name='all_submissions'),
]
