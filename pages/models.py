from django.db import models


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
