from django.core.management import call_command


def publish_finished_contests_job():
    call_command('publish_finished_contests')
