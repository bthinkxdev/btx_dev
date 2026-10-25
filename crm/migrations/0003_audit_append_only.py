"""Audit log rows can never be changed or removed, even by direct SQL (Part 13.5, 13.7).

SQLite and PostgreSQL both get BEFORE UPDATE / BEFORE DELETE triggers that abort the
statement. On PostgreSQL the application role should additionally be granted only
INSERT and SELECT on crm_auditlog (see docs/crm/DEPLOYMENT.md).
"""
from django.db import migrations

SQLITE_UP = [
    "CREATE TRIGGER IF NOT EXISTS crm_auditlog_no_update BEFORE UPDATE ON crm_auditlog "
    "BEGIN SELECT RAISE(ABORT, 'crm_auditlog is append-only'); END;",
    "CREATE TRIGGER IF NOT EXISTS crm_auditlog_no_delete BEFORE DELETE ON crm_auditlog "
    "BEGIN SELECT RAISE(ABORT, 'crm_auditlog is append-only'); END;",
    "CREATE TRIGGER IF NOT EXISTS crm_auditcheckpoint_no_update BEFORE UPDATE ON crm_auditcheckpoint "
    "BEGIN SELECT RAISE(ABORT, 'crm_auditcheckpoint is append-only'); END;",
    "CREATE TRIGGER IF NOT EXISTS crm_auditcheckpoint_no_delete BEFORE DELETE ON crm_auditcheckpoint "
    "BEGIN SELECT RAISE(ABORT, 'crm_auditcheckpoint is append-only'); END;",
]
SQLITE_DOWN = [
    'DROP TRIGGER IF EXISTS crm_auditlog_no_update;', 'DROP TRIGGER IF EXISTS crm_auditlog_no_delete;',
    'DROP TRIGGER IF EXISTS crm_auditcheckpoint_no_update;', 'DROP TRIGGER IF EXISTS crm_auditcheckpoint_no_delete;',
]
PG_UP = [
    """CREATE OR REPLACE FUNCTION crm_append_only() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege'; END;
       $$ LANGUAGE plpgsql;""",
    'DROP TRIGGER IF EXISTS crm_auditlog_append_only ON crm_auditlog;',
    'CREATE TRIGGER crm_auditlog_append_only BEFORE UPDATE OR DELETE ON crm_auditlog FOR EACH ROW EXECUTE FUNCTION crm_append_only();',
    'DROP TRIGGER IF EXISTS crm_auditlog_no_truncate ON crm_auditlog;',
    'CREATE TRIGGER crm_auditlog_no_truncate BEFORE TRUNCATE ON crm_auditlog FOR EACH STATEMENT EXECUTE FUNCTION crm_append_only();',
    'DROP TRIGGER IF EXISTS crm_auditcheckpoint_append_only ON crm_auditcheckpoint;',
    'CREATE TRIGGER crm_auditcheckpoint_append_only BEFORE UPDATE OR DELETE ON crm_auditcheckpoint FOR EACH ROW EXECUTE FUNCTION crm_append_only();',
]
PG_DOWN = [
    'DROP TRIGGER IF EXISTS crm_auditlog_append_only ON crm_auditlog;',
    'DROP TRIGGER IF EXISTS crm_auditlog_no_truncate ON crm_auditlog;',
    'DROP TRIGGER IF EXISTS crm_auditcheckpoint_append_only ON crm_auditcheckpoint;',
    'DROP FUNCTION IF EXISTS crm_append_only();',
]


def _run(statements_by_vendor):
    def run(apps, schema_editor):
        statements = statements_by_vendor.get(schema_editor.connection.vendor, [])
        for sql in statements:
            schema_editor.execute(sql, None)
    return run


class Migration(migrations.Migration):
    dependencies = [('crm', '0002_admin_dashboards_schedules')]

    operations = [
        migrations.RunPython(_run({'sqlite': SQLITE_UP, 'postgresql': PG_UP}), _run({'sqlite': SQLITE_DOWN, 'postgresql': PG_DOWN})),
    ]
