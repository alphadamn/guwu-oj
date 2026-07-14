from django.core.management.base import BaseCommand

from devlog import scanner


class Command(BaseCommand):
    help = '扫描 guwu-oj 项目文件，记录新增/修改/删除的改动（可用于 cron 定时任务）。'

    def add_arguments(self, parser):
        parser.add_argument(
            '--baseline-changes',
            action='store_true',
            help='首次扫描时也把所有文件记录为「新增」改动（默认仅建立基线）。',
        )

    def handle(self, *args, **options):
        result = scanner.scan(record_baseline_changes=options['baseline_changes'])
        if result['first_run'] and not options['baseline_changes']:
            self.stdout.write(self.style.SUCCESS(
                f"已建立基线快照：{result['total_tracked']} 个文件（未记录变更）。"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"扫描完成 — 新增 {result['added']}，修改 {result['modified']}，"
                f"删除 {result['deleted']}，共追踪 {result['total_tracked']} 个文件。"
            ))
