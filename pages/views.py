import logging

from django.conf import settings
from django.core.mail import send_mail
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import ensure_csrf_cookie
from .forms import ContactForm

logger = logging.getLogger(__name__)


def index(request):
    return render(request, 'pages/index.html')


def services(request):
    return render(request, 'pages/services.html')


def portfolio(request):
    return render(request, 'pages/portfolio.html')


def about(request):
    return render(request, 'pages/about.html')


def blog(request):
    return render(request, 'pages/blog.html')


@ensure_csrf_cookie
def contact(request):
    return render(request, 'pages/contact.html')


@require_http_methods(['POST'])
def contact_submit(request):
    """Accept contact form POST; validate, save, email notification, return JSON for AJAX."""
    form = ContactForm(request.POST)
    if form.is_valid():
        submission = form.save()
        _send_contact_notification(submission)
        return JsonResponse({'success': True, 'message': 'Thank you. We will get back to you within 24 hours.'})
    errors = {k: v[0] for k, v in form.errors.items()}
    logger.warning('Contact form validation failed: %s', errors)
    return JsonResponse({'success': False, 'errors': errors}, status=400)


def _send_contact_notification(submission):
    """Email contact form data to CONTACT_EMAIL_TO."""
    to_email = getattr(settings, 'CONTACT_EMAIL_TO', 'achujosephsl@gmail.com')
    subject = f'[BThinkX] New contact: {submission.name} — {submission.project_type}'
    body = (
        f"New contact form submission from BThinkX Dev website.\n\n"
        f"Name: {submission.name}\n"
        f"Email: {submission.email}\n"
        f"Company: {submission.company or '(not provided)'}\n"
        f"Budget: {submission.budget or '(not provided)'}\n"
        f"Project type: {submission.project_type}\n"
        f"Timeline: {submission.timeline or '(not provided)'}\n"
        f"Submitted: {submission.created_at.strftime('%Y-%m-%d %H:%M')} UTC\n\n"
        f"Message:\n{submission.message}"
    )
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[to_email],
        fail_silently=False,
    )


def page_404(request, exception=None):
    return render(request, 'pages/404.html', status=404)
