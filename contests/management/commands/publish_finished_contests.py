from django.core.management.base import BaseCommand
from django.utils import timezone
from contests.models import Contest


class Command(BaseCommand):
    help = 'Publish finished contest problems as normal public problems.'

    def handle(self, *args, **options):
        count = 0
        for contest in Contest.objects.filter(end_at__lte=timezone.now()):
            if contest.publish_finished_problems():
                count += 1
        self.stdout.write(self.style.SUCCESS(f'Published or repaired {count} finished contest(s).'))
