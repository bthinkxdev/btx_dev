"""
Staff Management: the one admin screen for creating/editing CRM accounts
and their EmployeeProfile (role, target, manager/lead-eligibility/WhatsApp-bot
flags, photo) — previously only reachable through Django admin.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import StaffCreateForm, StaffEditForm
from .models import EmployeeProfile, Lead
from .rbac import admin_required

User = get_user_model()
STAFF_PER_PAGE = 25


def _hx_toast(response, message):
    import json
    response['HX-Trigger'] = json.dumps({'crmToast': message})
    return response


def _staff_queryset():
    return (
        User.objects.select_related('crm_profile')
        .annotate(
            lead_count=Count('crm_leads', distinct=True),
            won_revenue=Sum(
                'crm_leads__deal_value',
                filter=Q(crm_leads__lifecycle_state=Lead.LifecycleState.WON),
            ),
        )
        .order_by('-is_active', 'first_name', 'username')
    )


@login_required
@admin_required
def staff_list(request):
    qs = _staff_queryset()

    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(
            Q(username__icontains=q) | Q(first_name__icontains=q)
            | Q(last_name__icontains=q) | Q(email__icontains=q)
        )

    role = (request.GET.get('role') or '').strip()
    role_choices = EmployeeProfile._meta.get_field('crm_role').choices
    if role in dict(role_choices):
        qs = qs.filter(crm_profile__crm_role=role)

    status = (request.GET.get('status') or '').strip()
    if status == 'active':
        qs = qs.filter(is_active=True)
    elif status == 'inactive':
        qs = qs.filter(is_active=False)

    paginator = Paginator(qs, STAFF_PER_PAGE)
    page_raw = (request.GET.get('page') or '1').strip()
    try:
        page_obj = paginator.page(page_raw)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages or 1)

    return render(request, 'crm/staff.html', {
        'page_obj': page_obj,
        'paginator': paginator,
        'role_choices': role_choices,
        'current_q': q,
        'current_role': role,
        'current_status': status,
        'total_staff': User.objects.count(),
        'active_staff': User.objects.filter(is_active=True).count(),
        'create_form': StaffCreateForm(),
    })


@login_required
@admin_required
@require_POST
def staff_create(request):
    form = StaffCreateForm(request.POST, request.FILES)
    if not form.is_valid():
        return render(request, 'crm/partials/staff_create_errors.html', {'form': form}, status=400)

    cd = form.cleaned_data
    with transaction.atomic():
        user = User.objects.create_user(
            username=cd['username'],
            email=cd['email'],
            password=cd['password'],
            first_name=cd['first_name'],
            last_name=cd['last_name'],
        )
        # The User post_save signal (crm.signals.ensure_crm_profile) already
        # created the EmployeeProfile — fill in what the form actually asked for.
        profile = user.crm_profile
        profile.crm_role = cd['crm_role']
        profile.target_amount = cd['target_amount'] or 0
        profile.is_sales_manager = cd['is_sales_manager']
        profile.eligible_for_leads = cd['eligible_for_leads']
        profile.whatsapp_bot_enabled = cd['whatsapp_bot_enabled']
        if cd.get('photo'):
            profile.photo = cd['photo']
        profile.save()

    resp = HttpResponse(status=204)
    resp['HX-Trigger'] = '{"crmToast": "Staff member added", "crmStaffRefresh": true}'
    return resp


@login_required
@admin_required
def staff_edit_panel(request, pk):
    user = get_object_or_404(User.objects.select_related('crm_profile'), pk=pk)
    profile = user.crm_profile
    form = StaffEditForm(initial={
        'first_name': user.first_name,
        'last_name': user.last_name,
        'email': user.email,
        'is_active': user.is_active,
        'crm_role': profile.crm_role,
        'target_amount': profile.target_amount,
        'is_sales_manager': profile.is_sales_manager,
        'eligible_for_leads': profile.eligible_for_leads,
        'whatsapp_bot_enabled': profile.whatsapp_bot_enabled,
    })
    return render(request, 'crm/partials/staff_edit_modal.html', {
        'staff_user': user, 'form': form, 'is_self': user.pk == request.user.pk,
    })


@login_required
@admin_required
@require_POST
def staff_update(request, pk):
    user = get_object_or_404(User.objects.select_related('crm_profile'), pk=pk)
    form = StaffEditForm(request.POST, request.FILES)
    if not form.is_valid():
        return render(request, 'crm/partials/staff_edit_modal.html', {
            'staff_user': user, 'form': form, 'is_self': user.pk == request.user.pk,
        }, status=400)

    cd = form.cleaned_data
    user.first_name = cd['first_name']
    user.last_name = cd['last_name']
    user.email = cd['email']
    if user.pk != request.user.pk:
        # Never let this screen lock the acting admin out of their own account.
        user.is_active = cd['is_active']
    if cd.get('new_password'):
        user.set_password(cd['new_password'])
    user.save()

    profile = user.crm_profile
    profile.crm_role = cd['crm_role']
    profile.target_amount = cd['target_amount'] or 0
    profile.is_sales_manager = cd['is_sales_manager']
    profile.eligible_for_leads = cd['eligible_for_leads']
    profile.whatsapp_bot_enabled = cd['whatsapp_bot_enabled']
    if cd.get('photo'):
        profile.photo = cd['photo']
    profile.save()

    resp = HttpResponse(status=204)
    resp['HX-Trigger'] = '{"crmToast": "Saved", "crmStaffRefresh": true}'
    return resp


@login_required
@admin_required
@require_POST
def staff_toggle_active(request, pk):
    user = get_object_or_404(User, pk=pk)
    if user.pk == request.user.pk:
        return HttpResponse('You cannot deactivate your own account.', status=400)
    user.is_active = not user.is_active
    user.save(update_fields=['is_active'])
    row = render(request, 'crm/partials/staff_row.html', {
        'row_user': User.objects.select_related('crm_profile').annotate(
            lead_count=Count('crm_leads', distinct=True),
            won_revenue=Sum('crm_leads__deal_value', filter=Q(crm_leads__lifecycle_state=Lead.LifecycleState.WON)),
        ).get(pk=user.pk),
    })
    _hx_toast(row, 'Activated' if user.is_active else 'Deactivated')
    return row
