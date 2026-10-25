from django.core.management.base import BaseCommand

from crm.core import jobs


class Command(BaseCommand):
    help = 'Run scheduled CRM jobs. Cron every 5 minutes:  */5 * * * * python manage.py crm_run_jobs'

    def add_arguments(self, parser):
        parser.add_argument('--group', default='frequent', choices=['frequent', 'daily', 'all'])
        parser.add_argument('--job', help='Run one job by name (ignores the time slot).')

    def handle(self, *args, group, job, **opts):
        if job:
            results = [jobs.run_job(job, force=True)]
        else:
            results = jobs.run_group(group)
            if group == 'frequent':
                results += jobs.run_group('daily')   # daily jobs only run once per day thanks to slots
        for r in results:
            style = self.style.SUCCESS if r['status'] == 'succeeded' else (
                self.style.WARNING if r['status'] == 'skipped' else self.style.ERROR)
            self.stdout.write(style(f"{r['job']}: {r['status']}"))
