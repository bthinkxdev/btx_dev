"""
Lead Finder storage. Fully self-contained: no foreign keys to (or from) any other app's tables.
The person who started a run is stored as a plain id + display name snapshot.
"""
from django.db import models


class SearchRun(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        STOPPING = 'stopping', 'Stopping'
        STOPPED = 'stopped', 'Stopped'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    ACTIVE_STATUSES = (Status.PENDING, Status.RUNNING, Status.STOPPING)

    location = models.CharField(max_length=150)
    category = models.CharField(max_length=150)
    target_count = models.PositiveIntegerField(default=50)
    min_reviews = models.PositiveIntegerField(default=3, help_text='Skip listings with fewer reviews (dead/new listings).')
    max_reviews = models.PositiveIntegerField(null=True, blank=True, default=500,
                                              help_text='Skip listings with more reviews (large businesses). Blank = no ceiling.')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    stop_requested = models.BooleanField(default=False)

    discovered_count = models.PositiveIntegerField(default=0)
    new_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    invalid_count = models.PositiveIntegerField(default=0)

    error_message = models.CharField(max_length=255, blank=True)
    created_by_id = models.PositiveIntegerField(null=True, blank=True)
    created_by_name = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return f'Run #{self.pk}: {self.category} in {self.location}'

    @property
    def remaining_target(self) -> int:
        return max(self.target_count - self.new_count, 0)


class Business(models.Model):
    """Every business ever seen, whatever its outcome. This table is the duplicate-prevention history."""

    class Opportunity(models.TextChoices):
        VERY_HIGH = 'very_high', 'Very High'
        HIGH = 'high', 'High'
        MEDIUM = 'medium', 'Medium'
        LOW = 'low', 'Low'
        UNKNOWN = 'unknown', 'Unknown'

    google_place_id = models.CharField(max_length=255, unique=True)
    phone_key = models.CharField(max_length=15, blank=True, db_index=True,
                                 help_text='Last 10 digits of the phone number, used to catch the same business under a different place id.')
    name = models.CharField(max_length=255)
    name_key = models.CharField(max_length=255, blank=True, db_index=True,
                                help_text='Normalised name with branch suffixes removed; same key = same brand (chain detection).')
    category = models.CharField(max_length=150, blank=True)
    search_location = models.CharField(max_length=150, blank=True)
    address = models.CharField(max_length=500, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    website = models.CharField(max_length=500, blank=True)
    maps_uri = models.CharField(max_length=500, blank=True)
    primary_type = models.CharField(max_length=100, blank=True)
    rating = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    review_count = models.PositiveIntegerField(null=True, blank=True)
    business_status = models.CharField(max_length=40, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    website_status = models.CharField(max_length=20, blank=True)
    instagram_url = models.CharField(max_length=500, blank=True)
    facebook_url = models.CharField(max_length=500, blank=True)
    social_presence_type = models.CharField(max_length=30, blank=True)

    qualified = models.BooleanField(default=False)
    business_quality_score = models.PositiveSmallIntegerField(null=True, blank=True)
    website_opportunity_score = models.PositiveSmallIntegerField(null=True, blank=True)
    website_opportunity_label = models.CharField(max_length=10, choices=Opportunity.choices, default=Opportunity.UNKNOWN)
    meta_opportunity_score = models.PositiveSmallIntegerField(null=True, blank=True)
    meta_opportunity_label = models.CharField(max_length=10, choices=Opportunity.choices, default=Opportunity.UNKNOWN)
    overall_score = models.PositiveSmallIntegerField(null=True, blank=True)
    reason = models.CharField(max_length=255, blank=True)

    first_run = models.ForeignKey(SearchRun, null=True, blank=True, on_delete=models.SET_NULL, related_name='first_found')
    times_seen = models.PositiveIntegerField(default=1)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-id']
        verbose_name_plural = 'businesses'

    def __str__(self):
        return self.name


class RunResult(models.Model):
    """One row per candidate a run looked at: what happened to it in that run."""

    class Outcome(models.TextChoices):
        NEW = 'new', 'New'
        DUPLICATE = 'duplicate', 'Already in history'
        REJECTED = 'rejected', 'Rejected'
        INVALID = 'invalid', 'Invalid'

    run = models.ForeignKey(SearchRun, on_delete=models.CASCADE, related_name='results')
    business = models.ForeignKey(Business, null=True, blank=True, on_delete=models.SET_NULL, related_name='results')
    name = models.CharField(max_length=255, blank=True)
    outcome = models.CharField(max_length=10, choices=Outcome.choices, db_index=True)
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']


class BlockedBrand(models.Model):
    """Brands (large chains/corporates) the finder never collects. Matched against the business name."""
    name = models.CharField(max_length=150)
    name_key = models.CharField(max_length=150, unique=True)
    added_by_name = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name
