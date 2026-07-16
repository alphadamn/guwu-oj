from django.urls import path
from .views import add_contest_question, contest_detail, contest_list, contest_question, contest_standings, create_contest, submit_contest_solution

urlpatterns = [
    path('', contest_list, name='contest_list'),
    path('create/', create_contest, name='create_contest'),
    path('<int:contest_id>/', contest_detail, name='contest_detail'),
    path('<int:contest_id>/questions/add/', add_contest_question, name='add_contest_question'),
    path('<int:contest_id>/standings/', contest_standings, name='contest_standings'),
    path('<int:contest_id>/question/<int:question_id>/', contest_question, name='contest_question'),
    path('<int:contest_id>/question/<int:question_id>/submit/', submit_contest_solution, name='submit_contest_solution'),
]
