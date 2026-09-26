import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse

from users.sliding_window import sliding_ratelimit as ratelimit

from .emails import notify_staff_ticket
from .forms import TicketForm
from .models import Ticket

logger = logging.getLogger(__name__)


@login_required
@ratelimit(key='user', rate='5/h', method='POST', block=True)
@ratelimit(key='ip', rate='10/h', method='POST', block=True)
def ticket_create(request):
    if request.method == 'POST':
        form = TicketForm(request.POST)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.submitter = request.user
            if not ticket.contact_email:
                ticket.contact_email = request.user.email or ''
            with transaction.atomic():
                ticket.save()
                admin_url = request.build_absolute_uri(
                    reverse('admin:tickets_ticket_change', args=[ticket.pk])
                )
                transaction.on_commit(lambda: notify_staff_ticket(ticket.pk, admin_url))
            messages.success(request, '工单已提交，管理员会尽快处理，感谢反馈！')
            return redirect('tickets:my_tickets')
    else:
        form = TicketForm(initial={'contact_email': request.user.email or ''})
    return render(request, 'tickets/submit.html', {'form': form})


@login_required
def my_tickets(request):
    tickets = Ticket.objects.filter(submitter=request.user)
    paginator = Paginator(tickets, 20)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'tickets/my_tickets.html', {'page_obj': page_obj})
