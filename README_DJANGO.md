# BThinkX Dev — Django project

This folder contains a Django project that serves the BThinkX template and a working contact form.

## Setup

1. **Create a virtual environment (recommended):**
   ```bash
   python -m venv venv
   venv\Scripts\activate   # Windows
   # or: source venv/bin/activate   # macOS/Linux
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Run migrations:**
   ```bash
   python manage.py migrate
   ```

4. **Create a superuser (optional, for admin):**
   ```bash
   python manage.py createsuperuser
   ```

5. **Run the development server:**
   ```bash
   python manage.py runserver
   ```
   Open http://127.0.0.1:8000/

## Project structure

- **config/** — Django project settings, root URLs, WSGI/ASGI.
- **pages/** — Main app: views (index, services, portfolio, about, blog, contact), contact form submission, `ContactSubmission` model.
- **templates/pages/** — Django templates (generated from the `new/` HTML); use `{% static %}` and `{% url 'pages:...' %}`.
- **new/** — Original static template; also used as `STATICFILES_DIRS` so `/static/assets/` serves `new/assets/`.

## Contact form

- **URL:** `/contact/` — contact page.
- **Submit URL:** `/contact/submit/` (POST).
- Submissions are saved to the database (model `ContactSubmission`).
- In development, emails are not sent (console backend). Configure `EMAIL_*` in settings or env for production.

## Admin

- URL: http://127.0.0.1:8000/admin/
- Log in with the superuser account. You can view and manage **Contact submissions** there.

## Rebuilding templates from `new/`

If you change the HTML in `new/*.html` and want to refresh Django templates:

```bash
python build_templates.py
```

Then re-apply any Django-specific changes (e.g. `{% csrf_token %}` and the contact form script in `templates/pages/contact.html`).
