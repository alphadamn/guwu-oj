from django.urls import path
from .views import home, problem_list, problem_detail, create_problem, leaderboard, solution_list, solution_detail, create_solution, like_solution

urlpatterns = [
    path('', home, name='home'),
    path('problems/', problem_list, name='problem_list'),
    path('problems/create/', create_problem, name='create_problem'),
    path('problem/<int:problem_id>/', problem_detail, name='problem_detail'),
    path('problem/<int:problem_id>/solutions/', solution_list, name='solution_list'),
    path('problem/<int:problem_id>/solution/<int:solution_id>/', solution_detail, name='solution_detail'),
    path('problem/<int:problem_id>/solution/create/', create_solution, name='create_solution'),
    path('problem/<int:problem_id>/solution/<int:solution_id>/like/', like_solution, name='like_solution'),
    path('leaderboard/', leaderboard, name='leaderboard'),
]
