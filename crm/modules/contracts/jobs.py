"""Contract jobs (Part 7.10, 9.6). Both are idempotent: schedule rows and alert levels are recorded."""
from .services import generate_recurring_invoices, renewal_alerts  # noqa: F401
