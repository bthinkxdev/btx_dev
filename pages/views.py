import logging

from django.conf import settings
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404, render
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import ensure_csrf_cookie

from .forms import ContactForm, JobApplicationForm, NewsletterSubscribeForm
from .models import (
    BlogPageSettings,
    BlogPost,
    JobApplication,
    JobPosting,
    NewsletterSubscriber,
    Project,
    TeamMember,
    TeamSection,
)

logger = logging.getLogger(__name__)

def index(request):
    homepage_projects = (
        Project.objects.filter(show_on_homepage=True)
        .filter(image__isnull=False)
        .exclude(image='')
        .order_by('sort_order', '-year', '-created_at')[:4]
    )
    return render(request, 'pages/index.html', {'homepage_projects': homepage_projects})

def services(request):
    return render(request, 'pages/services.html')

def portfolio(request):
    with_image = Project.objects.filter(image__isnull=False).exclude(image='')
    featured_project = (
        with_image.filter(is_featured=True)
        .order_by('sort_order', '-year', '-created_at')
        .first()
    )
    other_projects = with_image.order_by('sort_order', '-year', '-created_at')
    if featured_project:
        other_projects = other_projects.exclude(pk=featured_project.pk)

    context = {
        'featured_project': featured_project,
        'projects': other_projects,
    }
    return render(request, 'pages/portfolio.html', context)


def about(request):
    team_section = TeamSection.objects.first()
    team_members = (
        TeamMember.objects.filter(is_active=True)
        .filter(photo__isnull=False)
        .exclude(photo='')
        .order_by('sort_order', 'id')
    )
    return render(
        request,
        'pages/about.html',
        {
            'team_section': team_section,
            'team_members': team_members,
        },
    )


def blog(request):
    blog_settings = BlogPageSettings.objects.first()
    posts = list(
        BlogPost.objects.filter(is_published=True)
        .filter(featured_image__isnull=False)
        .exclude(featured_image='')
        .order_by('-published_at')
    )
    featured_post = None
    sidebar_posts = []
    grid_posts = []
    if posts:
        featured_post = next((p for p in posts if p.is_featured), None) or posts[0]
        rest = [p for p in posts if p.pk != featured_post.pk]
        sidebar_posts = rest[:3]
        grid_posts = rest[3:]

    listed = []
    if featured_post:
        listed.append(featured_post)
    listed.extend(sidebar_posts)
    listed.extend(grid_posts)
    seen_cat = set()
    blog_categories = []
    for p in sorted(listed, key=lambda x: (x.category.lower(), x.title)):
        if p.category_slug not in seen_cat:
            seen_cat.add(p.category_slug)
            blog_categories.append({'name': p.category, 'slug': p.category_slug})

    return render(
        request,
        'pages/blog.html',
        {
            'blog_settings': blog_settings,
            'featured_post': featured_post,
            'sidebar_posts': sidebar_posts,
            'grid_posts': grid_posts,
            'blog_categories': blog_categories,
        },
    )


@require_http_methods(['POST'])
def newsletter_subscribe(request):
    form = NewsletterSubscribeForm(request.POST)
    if form.is_valid():
        form.save()
        return JsonResponse(
            {
                'success': True,
                'message': "You're subscribed. We'll email you when we publish new articles.",
            }
        )
    err = form.errors.get('email', ['Enter a valid email address.'])[0]
    return JsonResponse({'success': False, 'message': str(err)}, status=400)


def newsletter_unsubscribe(request, token):
    sub = NewsletterSubscriber.objects.filter(unsubscribe_token=token).first()
    if not sub:
        return render(request, 'pages/newsletter_unsubscribed.html', {'ok': False}, status=404)
    if sub.is_active:
        sub.is_active = False
        sub.save(update_fields=['is_active'])
    return render(request, 'pages/newsletter_unsubscribed.html', {'ok': True})


def blog_post(request, slug):
    post = get_object_or_404(
        BlogPost.objects.filter(is_published=True),
        slug=slug,
    )
    return render(request, 'pages/blog_post.html', {'post': post})


@ensure_csrf_cookie
def contact(request):
    return render(request, 'pages/contact.html')


@ensure_csrf_cookie
def careers(request):
    jobs = JobPosting.objects.filter(is_published=True).order_by('sort_order', '-created_at')
    return render(request, 'pages/careers.html', {'jobs': jobs})


@require_http_methods(['POST'])
def career_apply(request):
    """Multipart application + resume; returns JSON for AJAX."""
    form = JobApplicationForm(request.POST, request.FILES)
    if form.is_valid():
        app = form.save()
        _send_career_notification(app, request)
        return JsonResponse(
            {
                'success': True,
                'message': 'Thanks! We received your application and will review it soon.',
            }
        )
    errors = {}
    for key, msgs in form.errors.items():
        errors[key] = msgs[0] if msgs else 'Invalid'
    logger.warning('Career application validation failed: %s', errors)
    return JsonResponse({'success': False, 'errors': errors}, status=400)


def _send_career_notification(app: JobApplication, request):
    to_email = getattr(settings, 'CONTACT_EMAIL_TO', 'hr@bthinkx.com')
    role = app.job.title if app.job else 'General / speculative'
    try:
        resume_url = request.build_absolute_uri(app.resume.url) if app.resume else ''
    except Exception:
        resume_url = app.resume.name if app.resume else ''
    subject = f'[BThinkX Careers] Application: {app.full_name} | {role}'
    body = (
        f"New job application from the Careers page.\n\n"
        f"Role: {role}\n"
        f"Name: {app.full_name}\n"
        f"Email: {app.email}\n"
        f"Phone: {app.phone}\n"
        f"LinkedIn: {app.linkedin_url or '(not provided)'}\n"
        f"Submitted: {app.created_at.strftime('%Y-%m-%d %H:%M')} UTC\n\n"
        f"Cover note:\n{app.cover_message or '(none)'}\n\n"
        f"Resume (download): {resume_url}\n"
        f"Admin: review in Django Admin → Job applications."
    )
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[to_email],
            fail_silently=False,
        )
    except Exception as e:
        logger.exception('Career notification email failed: %s', e)


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
    to_email = getattr(settings, 'CONTACT_EMAIL_TO', 'hr@bthinkx.com')
    subject = f'[BThinkX] New contact: {submission.name} | {submission.project_type}'
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
