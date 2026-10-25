"""Create demo users (one per role), a sales team, services and sample leads.

    python manage.py crm_demo_data --password 'choose-a-strong-one'

Never run this against production data.
"""
from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from crm.core import bootstrap, india

USERS = [
    ('owner', 'Veena', 'Owner', ['owner'], None),
    ('salesmanager', 'Meera', 'Nair', ['sales_manager'], 'Sales Kerala'),
    ('sales1', 'Rahul', 'Menon', ['sales_executive'], 'Sales Kerala'),
    ('sales2', 'Anjali', 'Pillai', ['sales_executive'], 'Sales Kerala'),
    ('telecaller', 'Arun', 'Das', ['telecaller'], None),
    ('finance', 'Fathima', 'Rahman', ['finance_executive'], None),
    ('finance2', 'Joseph', 'Mathew', ['finance_executive'], None),
    ('accountant', 'Suresh', 'Iyer', ['accountant'], None),
    ('hr', 'Lekha', 'Varma', ['hr_manager'], None),
    ('pm', 'Nikhil', 'Raj', ['project_manager'], None),
    ('dev', 'Divya', 'Krishnan', ['team_member'], None),
    ('auditor', 'Arjun', 'Kumar', ['auditor'], None),
]

SERVICES = [
    ('Online Store', 'online-store', 'Websites', '34999', '998314', 'one_time'),
    ('Business Website', 'business-website', 'Websites', '19999', '998314', 'one_time'),
    ('Meta Ads Management', 'meta-ads', 'Marketing', '15000', '998365', 'monthly'),
    ('SEO Retainer', 'seo-retainer', 'Marketing', '12000', '998365', 'monthly'),
    ('Website AMC', 'website-amc', 'Support', '6000', '998713', 'amc'),
    ('Custom Software', 'custom-software', 'Software', '150000', '998314', 'milestone'),
]

FIRST = ['Anita', 'Biju', 'Chitra', 'Deepak', 'Elsa', 'Faisal', 'Gopika', 'Hari', 'Irfan', 'Jaya', 'Kiran', 'Latha',
         'Manoj', 'Nisha', 'Omana', 'Prakash', 'Rekha', 'Sajan', 'Thomas', 'Uma']
LAST = ['K.', 'Nair', 'Menon', 'Pillai', 'Joseph', 'Varghese', 'Rahman', 'Kurian', 'Das', 'Iyer']
COMPANIES = ['Textiles', 'Bakers', 'Traders', 'Clinic', 'Boutique', 'Motors', 'Spices', 'Studio', 'Academy', 'Foods']
CITIES = ['Trivandrum', 'Kochi', 'Kozhikode', 'Thrissur', 'Kollam', 'Bengaluru', 'Chennai']


class Command(BaseCommand):
    help = 'Create demo users, team, services and leads for exploring the CRM.'

    def add_arguments(self, parser):
        parser.add_argument('--password', required=True, help='Password for every demo user (12+ characters).')
        parser.add_argument('--leads', type=int, default=30)

    @transaction.atomic
    def handle(self, *args, password, leads, **opts):
        if len(password) < 12:
            raise CommandError('Use a password of at least 12 characters.')
        from crm.models import LeadSource, Role, SalesTarget, Service, Team, UserProfile
        from crm.modules.leads import services as lead_svc
        bootstrap.run()
        User = get_user_model()
        team, _ = Team.objects.get_or_create(name='Sales Kerala')
        users = {}
        for username, first, last, roles, team_name in USERS:
            u, created = User.objects.get_or_create(username=username, defaults={
                'first_name': first, 'last_name': last, 'email': f'{username}@bthinkx.test'})
            if created:
                u.set_password(password)
                u.save()
            p, _ = UserProfile.objects.get_or_create(user=u)
            p.roles.set(Role.objects.filter(code__in=roles))
            if team_name:
                p.team = team
            p.receives_leads = 'sales_executive' in roles
            p.save()
            users[username] = u
        team.manager = users['salesmanager']
        team.save()
        for name, code, cat, price, sac, model in SERVICES:
            Service.objects.get_or_create(code=code, defaults={
                'name': name, 'category': cat, 'description': f'{name} for small businesses.',
                'deliverables': 'As per the agreed scope.', 'default_price': Decimal(price), 'sac_code': sac,
                'billing_model': model, 'floor_price': Decimal(price) * Decimal('0.8'),
                'cost_estimate': Decimal(price) * Decimal('0.45')})
        month = india.today().replace(day=1)
        for uname in ('sales1', 'sales2'):
            SalesTarget.objects.get_or_create(month=month, user=users[uname], defaults={
                'lead_count': 40, 'quotation_count': 15, 'won_value': Decimal('300000'), 'collected_value': Decimal('250000')})
        sources = list(LeadSource.objects.exclude(channel__in=['csv', 'existing_client']))
        services = list(Service.objects.all())
        rng = random.Random(42)
        made = 0
        for i in range(leads):
            name = f'{rng.choice(FIRST)} {rng.choice(LAST)}'
            phone = f'+919{rng.randint(100000000, 999999999)}'
            if lead_svc.find_duplicates(phone, ''):
                continue
            lead = lead_svc.create_lead(lead_svc.LeadInput(
                name=name, phone=phone, email=f'{name.split()[0].lower()}{i}@example.com',
                company=f'{name.split()[0]} {rng.choice(COMPANIES)}', city=rng.choice(CITIES), state='32',
                source=rng.choice(sources), priority=rng.choice(['hot', 'warm', 'warm', 'cold']),
                value=Decimal(rng.choice([19999, 34999, 49999, 120000])),
                next_follow_up_at=timezone.now() + timedelta(hours=rng.randint(-48, 96))),
                users['telecaller'], services=[rng.choice(services)], rule='round_robin')
            made += 1
        self.stdout.write(self.style.SUCCESS(
            f'Demo ready: {len(users)} users (password as given), {made} leads. Sign in at /crm/login/ as "owner".'))
