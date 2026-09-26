from django.urls import path

from . import views

app_name = 'tickets'

urlpatterns = [
    path('submit/', views.ticket_create, name='ticket_create'),
    path('mine/', views.my_tickets, name='my_tickets'),
]
