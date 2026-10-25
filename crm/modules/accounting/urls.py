from django.urls import path

from . import views

urlpatterns = [
    path('accounting/', views.dashboard, name='accounting_dashboard'),
    path('accounting/retry-postings/', views.retry_postings, name='retry_postings'),
    path('accounting/accounts/', views.account_list, name='account_list'),
    path('accounting/accounts/new/', views.account_form, name='account_create'),
    path('accounting/accounts/<int:pk>/edit/', views.account_form, name='account_edit'),
    path('accounting/accounts/<int:pk>/deactivate/', views.account_deactivate, name='account_deactivate'),
    path('accounting/journals/', views.JournalList.as_view(), name='journal_list'),
    path('accounting/journals/new/', views.journal_create, name='journal_create'),
    path('accounting/journals/<int:pk>/', views.journal_detail, name='journal_detail'),
    path('accounting/journals/<int:pk>/do/<slug:action>/', views.journal_action, name='journal_action'),
    path('accounting/ledger/', views.general_ledger, name='general_ledger'),
    path('accounting/trial-balance/', views.trial_balance, name='trial_balance'),
    path('accounting/profit-and-loss/', views.profit_loss, name='profit_loss'),
    path('accounting/balance-sheet/', views.balance_sheet, name='balance_sheet'),
    path('accounting/cash-flow/', views.cash_flow, name='cash_flow'),
    path('accounting/tax/', views.tax_reports, name='tax_reports'),
    path('accounting/periods/', views.period_list, name='period_list'),
    path('accounting/periods/<int:pk>/do/<slug:action>/', views.period_action, name='period_action'),
    path('banking/accounts/', views.bank_account_list, name='bank_account_list'),
    path('banking/reconciliation/', views.reconciliation, name='reconciliation'),
    path('banking/reconciliation/do/<slug:action>/', views.reconciliation_action, name='reconciliation_action'),
    path('banking/reconciliation/finalise/', views.reconciliation_finalise, name='reconciliation_finalise'),
    path('banking/reconciliation/<int:pk>/', views.reconciliation_report, name='reconciliation_report'),
    path('banking/reconciliation/<int:pk>/undo/', views.reconciliation_undo, name='reconciliation_undo'),
]
