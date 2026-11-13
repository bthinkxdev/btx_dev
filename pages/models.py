import uuid
from datetime import date, timedelta

from django.conf import settings
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class ContactSubmission(models.Model):
    """Stores contact form submissions from the website."""

    name = models.CharField(max_length=200)
    email = models.EmailField()
    company = models.CharField(max_length=200, blank=True)
    budget = models.CharField(max_length=50, blank=True)
    project_type = models.CharField(max_length=100)
    message = models.TextField()
    timeline = models.CharField(max_length=50, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    # Read/unread, surfaced in the CRM's Website Leads list.
    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Contact submission'
        verbose_name_plural = 'Contact submissions'

    def __str__(self):
        return f"{self.name} ({self.email}) @ {self.created_at:%Y-%m-%d %H:%M}"


class QuoteRequest(models.Model):
    """Lead captured before a pricing-package PDF quotation download."""

    PACKAGE_CHOICES = [
        ('online-store', 'Online Store · ₹34,999'),
        ('online-store-marketing', 'Online Store + Marketing · ₹49,999'),
        ('complete-growth', 'Complete Growth · ₹74,999'),
    ]

    BUSINESS_TYPE_CHOICES = [
        ('fashion-apparel', 'Fashion & Apparel'),
        ('grocery-fmcg', 'Grocery & FMCG'),
        ('electronics', 'Electronics'),
        ('beauty-wellness', 'Beauty & Wellness'),
        ('food-beverage', 'Food & Beverage'),
        ('home-furniture', 'Home & Furniture'),
        ('handicrafts-jewellery', 'Handicrafts & Jewellery'),
        ('other', 'Other'),
    ]

    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=30)
    business_name = models.CharField(max_length=200)
    business_type = models.CharField(max_length=40, choices=BUSINESS_TYPE_CHOICES)
    package = models.CharField(max_length=40, choices=PACKAGE_CHOICES)

    created_at = models.DateTimeField(auto_now_add=True)

    # Read/unread, surfaced in the CRM's Website Leads list.
    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Quote request'
        verbose_name_plural = 'Quote requests'

    def __str__(self):
        return f"{self.name} · {self.get_package_display()} @ {self.created_at:%Y-%m-%d %H:%M}"


class TechTag(models.Model):
    """Technology tags used to describe project stacks (e.g. Django, React)."""

    name = models.CharField(max_length=50, unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self) -> str:
        return self.name


class Project(models.Model):
    """Portfolio projects displayed in 'Products We've Shipped'."""

    CATEGORY_ECOMMERCE = 'ecommerce'
    CATEGORY_SAAS = 'saas'
    CATEGORY_SOFTWARE = 'software'
    CATEGORY_AUTOMATION = 'automation'

    CATEGORY_CHOICES = [
        (CATEGORY_ECOMMERCE, 'E-commerce'),
        (CATEGORY_SAAS, 'SaaS'),
        (CATEGORY_SOFTWARE, 'Software'),
        (CATEGORY_AUTOMATION, 'Automation'),
    ]

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    category = models.CharField(
        max_length=20,
        choices=CATEGORY_CHOICES,
        help_text="Used for filtering (ecommerce / saas / software / automation).",
    )
    year = models.PositiveIntegerField(blank=True, null=True)
    short_description = models.TextField()

    image = models.ImageField(
        upload_to='portfolio/',
        blank=True,
        null=True,
        help_text='Upload a screenshot or hero image (required for the site listing).',
    )

    stack = models.ManyToManyField(
        TechTag,
        related_name='projects',
        blank=True,
        help_text="Technologies used in this project.",
    )

    is_featured = models.BooleanField(
        default=False,
        help_text="If true, shown as a large featured card on the portfolio page.",
    )
    show_on_homepage = models.BooleanField(
        default=True,
        help_text="If true, project can appear in the homepage portfolio strip.",
    )
    is_published = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Untick to hide this work from the website without deleting it.",
    )

    sort_order = models.PositiveIntegerField(
        default=0,
        help_text="Manual ordering; lower numbers appear first.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sort_order', '-year', '-created_at']

    def __str__(self) -> str:
        return self.title


class TeamSection(models.Model):
    """Single row: copy for the About page “People Behind the Products” block."""

    badge = models.CharField(max_length=100, default='The Team')
    heading = models.CharField(max_length=200, default='People Behind the Products')
    subheading = models.TextField(
        default='A small, senior team of engineers, designers, and strategists who love building things that work.',
        help_text='Short intro shown under the heading.',
    )

    class Meta:
        verbose_name = 'About team section'
        verbose_name_plural = 'About team section'

    def __str__(self) -> str:
        return 'Team section (About page)'


class TeamMember(models.Model):
    """Team member card on the About page."""

    title = models.CharField(
        max_length=200,
        help_text='Job title on the card, e.g. Backend Engineering Lead',
    )
    role = models.CharField(
        max_length=200,
        help_text='Role line, e.g. Senior Django / Python Developer',
    )
    bio = models.TextField()
    photo = models.ImageField(
        upload_to='team/',
        help_text='Square or portrait photo works best.',
    )
    skills = models.TextField(
        blank=True,
        help_text='Comma-separated skill tags, e.g. Django, Python, AWS',
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(
        default=0,
        help_text='Lower numbers appear first.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sort_order', 'id']
        verbose_name = 'Team member'
        verbose_name_plural = 'Team members'

    def __str__(self) -> str:
        return self.title

    @property
    def skill_list(self):
        if not self.skills or not self.skills.strip():
            return []
        return [s.strip() for s in self.skills.split(',') if s.strip()]


class BlogPageSettings(models.Model):
    """Single row: hero + newsletter copy on the blog listing page."""

    hero_badge = models.CharField(max_length=100, default='Insights & Guides')
    hero_heading = models.CharField(max_length=200, default='The BThinkX Dev Blog')
    hero_subheading = models.TextField(
        default='Deep dives into e-commerce engineering, AI automation, modern web development, and scaling digital products.',
    )
    newsletter_badge = models.CharField(max_length=100, default='Stay Updated')
    newsletter_heading = models.CharField(max_length=200, default='Get Weekly Engineering Insights')
    newsletter_text = models.TextField(
        default='No fluff. Just practical deep dives on building scalable digital products, straight to your inbox every week.',
    )
    newsletter_subtext = models.CharField(
        max_length=300,
        blank=True,
        default='Join 1,200+ engineers and founders who already subscribe.',
    )

    class Meta:
        verbose_name = 'Blog page settings'
        verbose_name_plural = 'Blog page settings'

    def __str__(self) -> str:
        return 'Blog page settings'


class SiteSettings(models.Model):
    """Single row: site-wide switches editable from the admin."""

    diwali_enabled = models.BooleanField(
        'Diwali Special on',
        default=True,
        help_text='Shows the Diwali chip in the navbar and the Diwali strip, lamps and CTA on the home page.',
    )
    diwali_ends_on = models.DateField(
        'Diwali Special ends on',
        null=True,
        blank=True,
        help_text='Optional. The Diwali Special switches itself off after this date.',
    )
    diwali_date = models.DateField(
        'Diwali day',
        null=True,
        blank=True,
        default=date(2026, 11, 8),
        help_text='Drives the "days to Diwali" countdown on the home page. Leave empty to hide the countdown.',
    )
    diwali_glitter_enabled = models.BooleanField(
        'Golden glitter',
        default=True,
        help_text='Floating golden glitter and the sparkle trail that follows the cursor.',
    )
    diwali_crackers_enabled = models.BooleanField(
        'Crackers',
        default=True,
        help_text='Fireworks that burst when visitors click or tap, scroll, hover the offer button or send a form.',
    )

    class Meta:
        verbose_name = 'Site settings'
        verbose_name_plural = 'Site settings'

    CACHE_KEY = 'pages:site-settings'
    CACHE_SECONDS = 60

    def __str__(self) -> str:
        return 'Site settings'

    @classmethod
    def current(cls) -> 'SiteSettings':
        """The settings row (or an unsaved default one), cached so public pages do not query it each time."""
        from django.core.cache import cache
        from django.db import DatabaseError

        row = cache.get(cls.CACHE_KEY)
        if row is None:
            try:
                row = cls.objects.first() or cls()
            except DatabaseError:
                return cls()
            cache.set(cls.CACHE_KEY, row, cls.CACHE_SECONDS)
        return row

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from django.core.cache import cache
        cache.delete(self.CACHE_KEY)

    def delete(self, *args, **kwargs):
        result = super().delete(*args, **kwargs)
        from django.core.cache import cache
        cache.delete(self.CACHE_KEY)
        return result

    @property
    def diwali_active(self) -> bool:
        from django.utils import timezone

        if not self.diwali_enabled:
            return False
        return not (self.diwali_ends_on and timezone.localdate() > self.diwali_ends_on)

    @property
    def days_to_diwali(self):
        """Days left until Diwali day, 0 on the day itself, None when unset or already past."""
        if not self.diwali_date:
            return None
        days = (self.diwali_date - timezone.localdate()).days
        return days if days >= 0 else None


class BlogPost(models.Model):
    """Blog article for listing and detail pages."""

    title = models.CharField(max_length=300)
    slug = models.SlugField(max_length=320, unique=True)
    category = models.CharField(
        max_length=100,
        help_text='e.g. E-commerce, AI / Automation, Web Dev, used for filters.',
    )
    category_slug = models.SlugField(max_length=120, editable=False, db_index=True)
    excerpt = models.TextField(help_text='Short summary for cards and SEO.')
    body = models.TextField(help_text='Full article (shown on the post page).')
    featured_image = models.ImageField(upload_to='blog/')
    published_at = models.DateTimeField(db_index=True)
    read_time_minutes = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        help_text='Optional e.g. 12 for “12 min read”.',
    )
    is_featured = models.BooleanField(
        default=False,
        help_text='Pinned as the large hero card (use one at a time).',
    )
    is_published = models.BooleanField(default=True, db_index=True)
    meta_description = models.CharField(
        max_length=320,
        blank=True,
        help_text='Override for search/social; defaults to excerpt.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    notification_job_started_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text='Set when background newsletter send starts (prevents duplicate jobs).',
    )
    subscriber_notification_completed_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text='Set when the newsletter batch finishes (success or partial).',
    )

    class Meta:
        ordering = ['-published_at', '-id']
        verbose_name = 'Blog post'
        verbose_name_plural = 'Blog posts'

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs):
        self.category_slug = slugify(self.category)[:120] or 'general'
        super().save(*args, **kwargs)

    def get_meta_description(self) -> str:
        return (self.meta_description or self.excerpt or '')[:320]


class NewsletterSubscriber(models.Model):
    """Blog / insights email list."""

    email = models.EmailField(unique=True, db_index=True)
    unsubscribe_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    is_active = models.BooleanField(default=True, db_index=True)
    subscribed_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(
        max_length=50,
        blank=True,
        help_text='e.g. blog_footer',
    )

    class Meta:
        ordering = ['-subscribed_at']
        verbose_name = 'Newsletter subscriber'
        verbose_name_plural = 'Newsletter subscribers'

    def __str__(self) -> str:
        return self.email


def _career_resume_upload_to(instance, filename):
    ext = (filename.rsplit('.', 1)[-1] if '.' in filename else 'pdf').lower()[:5]
    if ext not in ('pdf', 'doc', 'docx'):
        ext = 'pdf'
    return f'careers/resumes/{timezone.now():%Y/%m}/{uuid.uuid4().hex}.{ext}'


class JobPosting(models.Model):
    """Open roles listed on the Careers page (managed in admin)."""

    TYPE_FULL = 'full_time'
    TYPE_PART = 'part_time'
    TYPE_CONTRACT = 'contract'
    TYPE_INTERN = 'internship'

    EMPLOYMENT_CHOICES = [
        (TYPE_FULL, 'Full-time'),
        (TYPE_PART, 'Part-time'),
        (TYPE_CONTRACT, 'Contract'),
        (TYPE_INTERN, 'Internship'),
    ]

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True)
    department = models.CharField(max_length=100, blank=True, help_text='e.g. Engineering, Design')
    location = models.CharField(max_length=120, default='Remote / Trivandrum')
    employment_type = models.CharField(max_length=20, choices=EMPLOYMENT_CHOICES, default=TYPE_FULL)
    summary = models.CharField(max_length=320, help_text='Short line shown on job cards')
    description = models.TextField(help_text='Full role description (line breaks are kept)')
    is_published = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveIntegerField(default=0, help_text='Lower = listed first')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', '-created_at']
        verbose_name = 'Job posting'
        verbose_name_plural = 'Job postings'

    def __str__(self) -> str:
        return self.title


class JobApplication(models.Model):
    """Candidate application + resume (submitted from Careers page)."""

    job = models.ForeignKey(
        JobPosting,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='applications',
        help_text='Empty = general / speculative application',
    )
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=40)
    linkedin_url = models.URLField(blank=True)
    cover_message = models.TextField(blank=True, max_length=2500)
    resume = models.FileField(
        upload_to=_career_resume_upload_to,
        validators=[FileExtensionValidator(['pdf', 'doc', 'docx'])],
        help_text='PDF, DOC, or DOCX, max 5MB',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # Hiring pipeline, worked from the CRM's Careers > Applications list.
    STATUS_NEW = 'new'
    STATUS_CHOICES = [
        ('new', 'New'), ('reviewing', 'Reviewing'), ('shortlisted', 'Shortlisted'),
        ('interview', 'Interview'), ('hired', 'Hired'), ('rejected', 'Rejected'),
    ]
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True)
    internal_notes = models.TextField(blank=True)
    acknowledged_at = models.DateTimeField(
        null=True, blank=True, help_text='When the automatic thank-you email was queued for the candidate.')

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Job application'
        verbose_name_plural = 'Job applications'

    def __str__(self) -> str:
        role = self.job.title if self.job else 'General'
        return f'{self.full_name} → {role} ({self.created_at:%Y-%m-%d})'


def _new_uid() -> str:
    return uuid.uuid4().hex


class Interview(models.Model):
    """An interview HR schedules for a job application from the CRM (candidate is emailed the details)."""

    MODE_MEET = 'google_meet'
    MODE_ZOOM = 'zoom'
    MODE_TEAMS = 'teams'
    MODE_PHONE = 'phone'
    MODE_IN_PERSON = 'in_person'
    MODE_CHOICES = [
        (MODE_MEET, 'Google Meet'),
        (MODE_ZOOM, 'Zoom'),
        (MODE_TEAMS, 'Microsoft Teams'),
        (MODE_PHONE, 'Phone call'),
        (MODE_IN_PERSON, 'In person at our office'),
    ]
    ONLINE_MODES = (MODE_MEET, MODE_ZOOM, MODE_TEAMS)

    STATUS_SCHEDULED = 'scheduled'
    STATUS_CHOICES = [
        ('scheduled', 'Scheduled'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('no_show', 'No show'),
    ]

    application = models.ForeignKey(JobApplication, on_delete=models.CASCADE, related_name='interviews')
    round_name = models.CharField(max_length=80, default='First interview')
    scheduled_at = models.DateTimeField(db_index=True)
    duration_minutes = models.PositiveSmallIntegerField(default=30)
    mode = models.CharField(max_length=12, choices=MODE_CHOICES, default=MODE_MEET)
    meeting_link = models.URLField(max_length=500, blank=True)
    location = models.CharField(max_length=300, blank=True, help_text='Office address, or the number we will call.')
    interviewers = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name='career_interviews')
    message = models.TextField(blank=True, help_text='Personal note included in the email to the candidate.')
    feedback = models.TextField(blank=True, help_text='Internal notes after the interview. The candidate never sees this.')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_SCHEDULED, db_index=True)
    uid = models.CharField(max_length=40, unique=True, default=_new_uid, editable=False)
    sequence = models.PositiveSmallIntegerField(default=0, editable=False, help_text='Calendar invite revision.')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    candidate_notified_at = models.DateTimeField(null=True, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['scheduled_at']
        verbose_name = 'Interview'
        verbose_name_plural = 'Interviews'

    def __str__(self) -> str:
        return f'{self.round_name}: {self.application.full_name} on {timezone.localtime(self.scheduled_at):%d %b %Y %H:%M}'

    @property
    def ends_at(self):
        return self.scheduled_at + timedelta(minutes=self.duration_minutes)

    @property
    def is_online(self) -> bool:
        return self.mode in self.ONLINE_MODES

    @property
    def is_upcoming(self) -> bool:
        return self.status == self.STATUS_SCHEDULED and self.scheduled_at >= timezone.now()


def _webinar_uid() -> str:
    return uuid.uuid4().hex


class Webinar(models.Model):
    """A paid live webinar shown on its own landing page (/webinar/). Everything on the page that changes from one
    session to the next lives here, so a new webinar is a new row, not a code change."""

    title = models.CharField(max_length=200, default='How to Sell Online 10x Better Than Your Competitors')
    slug = models.SlugField(max_length=80, unique=True, default='ecommerce-10x')
    subtitle = models.CharField(
        max_length=300,
        default='A live session for business owners on building your own digital sales system: your store, your ads and '
                'your social media.')
    starts_at = models.DateTimeField(help_text='Start time in IST.')
    duration_minutes = models.PositiveSmallIntegerField(default=90)
    price = models.DecimalField(max_digits=8, decimal_places=2, default=199)
    platform = models.CharField(max_length=60, default='Google Meet', help_text='Where it is held, e.g. Google Meet or Zoom.')
    join_url = models.URLField(
        max_length=500, blank=True,
        help_text='The joining link. It is emailed to everyone who has paid. You can add it later, then use '
                  '"Email joining link" in the CRM.')
    host_name = models.CharField(max_length=120, default='Team BThinkX')
    host_title = models.CharField(max_length=160, default='E-commerce and digital marketing company, Trivandrum')
    seat_limit = models.PositiveIntegerField(null=True, blank=True, help_text='Leave empty for no limit.')
    learn_points = models.TextField(
        blank=True, help_text='What attendees will learn. One per line, written as "Heading | one-line explanation".',
        default=(
            'Sell on your own terms | Why your own online store beats depending on marketplaces and DMs, and how to set '
            'one up that actually converts.\n'
            'How digital marketing really works | The simple Meta ads funnel from reach to order: targeting, budgets and '
            'retargeting.\n'
            'Run social media like a sales channel | What to post, how often, and how to turn comments and messages into '
            'orders.\n'
            'Fix what quietly loses you sales | Checkout friction, a slow website and missing tracking: the leaks your '
            'competitors never plug.\n'
            'Payments, tracking and automation | Collect payments by UPI, cards and COD, measure what works and automate '
            'the follow-ups.\n'
            'Live questions and answers | Bring your own business questions and get practical answers.'))
    audience_points = models.TextField(
        blank=True, help_text='Who it is for. One per line.',
        default=('Shop owners who take orders on WhatsApp and Instagram\n'
                 'Small brands that want to grow beyond their local customers\n'
                 'Business owners spending on ads without clear results\n'
                 'Anyone planning to start selling products online'))
    is_active = models.BooleanField(default=True, help_text='Untick to take the page offline.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-starts_at']
        verbose_name = 'Webinar'
        verbose_name_plural = 'Webinars'

    def __str__(self) -> str:
        return f'{self.title} ({timezone.localtime(self.starts_at):%d %b %Y %H:%M})'

    @property
    def ends_at(self):
        return self.starts_at + timedelta(minutes=self.duration_minutes)

    @property
    def registration_open(self) -> bool:
        return self.is_active and timezone.now() < self.starts_at

    @property
    def learn_items(self) -> list[tuple[str, str]]:
        out = []
        for line in self.learn_points.splitlines():
            line = line.strip()
            if not line:
                continue
            head, _, body = line.partition('|')
            out.append((head.strip(), body.strip()))
        return out

    @property
    def audience_items(self) -> list[str]:
        return [x.strip() for x in self.audience_points.splitlines() if x.strip()]

    def paid_count(self) -> int:
        return self.registrations.filter(status=WebinarRegistration.Status.PAID).count()

    def seats_left(self):
        return None if self.seat_limit is None else max(0, self.seat_limit - self.paid_count())


class WebinarRegistration(models.Model):
    """One attempt to register for a webinar. A paid row is a confirmed seat; a pending or failed row is a lead
    who started checkout and can be followed up."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PAID = 'paid', 'Paid'
        FAILED = 'failed', 'Failed'

    webinar = models.ForeignKey(Webinar, on_delete=models.PROTECT, related_name='registrations')
    name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=20)
    business_name = models.CharField(max_length=200, blank=True)
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    cashfree_order_id = models.CharField(max_length=80, unique=True, db_index=True)
    cashfree_payment_id = models.CharField(max_length=80, blank=True)

    # where the visitor came from (the ad), copied from the landing page URL
    utm_source = models.CharField(max_length=100, blank=True)
    utm_medium = models.CharField(max_length=100, blank=True)
    utm_campaign = models.CharField(max_length=150, blank=True)
    utm_content = models.CharField(max_length=150, blank=True)
    ad_click_id = models.CharField(max_length=200, blank=True)

    # browser details kept so the Meta Conversions API can match a server-side purchase to the ad click
    # (the payment is confirmed by a webhook, when the visitor's browser is no longer there to ask)
    fbp = models.CharField(max_length=120, blank=True)
    fbc = models.CharField(max_length=300, blank=True)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)
    source_url = models.CharField(max_length=500, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    confirmation_sent_at = models.DateTimeField(null=True, blank=True)
    joining_sent_at = models.DateTimeField(null=True, blank=True, help_text='When the joining link was last emailed.')
    reminder_24h_sent_at = models.DateTimeField(null=True, blank=True)
    reminder_1h_sent_at = models.DateTimeField(null=True, blank=True)
    attended = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Webinar registration'
        verbose_name_plural = 'Webinar registrations'

    def __str__(self) -> str:
        return f'{self.name} - {self.webinar.slug} ({self.get_status_display()})'


class MetaTracking(models.Model):
    """The Meta pixel and Conversions API settings, edited by an administrator in the CRM (one row). Anything left empty
    here falls back to the server environment (META_* variables), so existing deployments keep working. The access token
    is stored encrypted and is write-only: it is never shown again."""

    pixel_enabled = models.BooleanField(default=True, help_text='Run the pixel in the visitor browser.')
    pixel_id = models.CharField(max_length=30, blank=True, help_text='The pixel (dataset) ID from Events Manager.')
    capi_enabled = models.BooleanField(default=True, help_text='Also send Lead, InitiateCheckout and Purchase from the server.')
    capi_token_enc = models.TextField(blank=True, editable=False)
    token_updated_at = models.DateTimeField(null=True, blank=True, editable=False)
    test_event_code = models.CharField(max_length=40, blank=True)
    graph_version = models.CharField(max_length=10, default='v21.0')
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    class Meta:
        verbose_name = 'Meta tracking settings'
        verbose_name_plural = 'Meta tracking settings'

    def __str__(self) -> str:
        return 'Meta tracking settings'

    @classmethod
    def current(cls) -> 'MetaTracking':
        return cls.objects.get_or_create(pk=1)[0]


class MetaEvent(models.Model):
    """One server-side event for Meta's Conversions API: an outbox row, so an event is sent once, retried when Meta or the
    network fails, and can be inspected afterwards. (event_name, event_id) is what Meta uses to merge the server event with
    the same event fired by the browser pixel."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Queued'
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'

    registration = models.ForeignKey(WebinarRegistration, on_delete=models.CASCADE, related_name='meta_events')
    event_name = models.CharField(max_length=40)
    event_id = models.CharField(max_length=80)
    event_time = models.DateTimeField()
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['event_name', 'event_id'], name='uniq_meta_event')]
        indexes = [models.Index(fields=['status', 'created_at'])]

    def __str__(self) -> str:
        return f'{self.event_name} {self.event_id} ({self.status})'


class OfferSignup(models.Model):
    """A paid signup from a limited-seats campaign landing page (e.g. the
    Navaratri offer). One row per attempt — a retried/abandoned payment just
    leaves an earlier row PENDING/FAILED rather than overwriting anything."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PAID = 'paid', 'Paid'
        FAILED = 'failed', 'Failed'

    class Plan(models.TextChoices):
        STANDARD = 'standard', 'Up to 100 products'
        UNLIMITED = 'unlimited', 'Unlimited products'

    offer_slug = models.SlugField(max_length=60, db_index=True)
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20)
    place = models.CharField(max_length=200)
    business_name = models.CharField(max_length=200)

    plan = models.CharField(max_length=10, choices=Plan.choices, default=Plan.STANDARD)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)

    cashfree_order_id = models.CharField(max_length=80, unique=True, db_index=True)
    cashfree_payment_id = models.CharField(max_length=80, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Offer signup'
        verbose_name_plural = 'Offer signups'

    def __str__(self) -> str:
        return f'{self.business_name} — {self.offer_slug} ({self.get_status_display()})'
