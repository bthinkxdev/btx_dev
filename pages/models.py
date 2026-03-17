import uuid

from django.db import models
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

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Contact submission'
        verbose_name_plural = 'Contact submissions'

    def __str__(self):
        return f"{self.name} ({self.email}) — {self.created_at:%Y-%m-%d %H:%M}"


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
        verbose_name = 'About — team section'
        verbose_name_plural = 'About — team section'

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
        default='No fluff. Just practical deep dives on building scalable digital products — straight to your inbox every week.',
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


class BlogPost(models.Model):
    """Blog article for listing and detail pages."""

    title = models.CharField(max_length=300)
    slug = models.SlugField(max_length=320, unique=True)
    category = models.CharField(
        max_length=100,
        help_text='e.g. E-commerce, AI / Automation, Web Dev — used for filters.',
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
