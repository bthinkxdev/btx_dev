"""
Re-renders the PDF for every existing Bill (and BillPayment receipt) using
the current render_bill_pdf / render_payment_receipt_pdf code.

Needed once after a change to the PDF layout/branding (e.g. the seal and
signature transparency fix) — bills created before that change still have
their old, stale PDF sitting on disk until this runs. Safe to re-run.
"""

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from crm.models import Bill, BillPayment
from crm.services.simple_billing import render_bill_pdf, render_payment_receipt_pdf


class Command(BaseCommand):
    help = 'Re-render the PDF for every existing Bill and BillPayment receipt.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Report counts; write nothing.')

    def handle(self, *args, **options):
        dry = options['dry_run']
        prefix = '[dry-run] ' if dry else ''

        bills_done = 0
        for bill in Bill.objects.all():
            if dry:
                bills_done += 1
                continue
            if bill.pdf_file:
                bill.pdf_file.delete(save=False)
            bill.pdf_file.save(
                f'{bill.bill_number.replace("/", "-")}.pdf',
                ContentFile(render_bill_pdf(bill)),
                save=True,
            )
            bills_done += 1

        payments_done = 0
        for payment in BillPayment.objects.filter(bill__isnull=False, status=BillPayment.Status.VERIFIED):
            if dry:
                payments_done += 1
                continue
            if not payment.receipt_number:
                continue  # legacy payment predating per-payment receipts — nothing to regenerate from
            if payment.receipt_pdf:
                payment.receipt_pdf.delete(save=False)
            payment.receipt_pdf.save(
                f'{payment.receipt_number.replace("/", "-")}.pdf',
                ContentFile(render_payment_receipt_pdf(payment.bill, payment)),
                save=True,
            )
            payments_done += 1

        self.stdout.write(self.style.SUCCESS(
            f'{prefix}Bills regenerated: {bills_done}, payment receipts regenerated: {payments_done}'
        ))
