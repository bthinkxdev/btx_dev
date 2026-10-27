"""Export CRM data from a SQLite backup into an Excel workbook (read-only on the DB).

Usage:
    python export_crm_backup_to_excel.py <backup.sqlite3> [output.xlsx]

- A "Leads" sheet joins each lead with its owning executive, package and place.
- One sheet per non-empty crm_* table; auth_user foreign keys are shown as usernames.
"""
import sqlite3
import sys
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter


def write_sheet(wb, title, headers, rows):
    ws = wb.create_sheet(title[:31])
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append(list(row))
    ws.freeze_panes = "A2"
    for i, h in enumerate(headers, 1):
        width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows[:200] if r[i - 1] is not None])
        ws.column_dimensions[get_column_letter(i)].width = min(width + 2, 60)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else f"crm_export_{datetime.now():%Y%m%d_%H%M%S}.xlsx"

    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    users = {}
    for uid, uname, first, last, email in con.execute(
        "select id, username, first_name, last_name, email from auth_user"
    ):
        users[uid] = uname

    wb = Workbook()
    wb.remove(wb.active)

    # Leads with owner executive, package and place details
    place_cols = [r[1] for r in con.execute("pragma table_info(crm_place)") if r[1] != "id"]
    lead_cols = [r[1] for r in con.execute("pragma table_info(crm_lead)")]
    sel = ", ".join(
        [f"l.{c}" for c in lead_cols]
        + ["u.username", "u.first_name", "u.last_name", "pk.name", "pk.price"]
        + [f"pl.{c}" for c in place_cols]
    )
    rows = con.execute(
        f"select {sel} from crm_lead l "
        "left join auth_user u on u.id = l.employee_id "
        "left join crm_package pk on pk.id = l.package_id "
        "left join crm_place pl on pl.id = l.place_id order by l.id"
    ).fetchall()
    headers = (
        lead_cols
        + ["executive_username", "executive_first_name", "executive_last_name", "package_name", "package_price"]
        + [f"place_{c}" for c in place_cols]
    )
    write_sheet(wb, "Leads", headers, rows)

    # Every other non-empty crm_* table
    tables = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table' and name like 'crm_%' order by 1")]
    for t in tables:
        cols = [r[1] for r in con.execute(f'pragma table_info("{t}")')]
        user_fks = {r[3] for r in con.execute(f'pragma foreign_key_list("{t}")') if r[2] == "auth_user"}
        rows = con.execute(f'select * from "{t}"').fetchall()
        if not rows:
            continue
        idx = [cols.index(c) for c in user_fks]
        fixed = []
        for r in rows:
            r = list(r)
            for i in idx:
                r[i] = users.get(r[i], r[i])
            fixed.append(r)
        write_sheet(wb, t.removeprefix("crm_"), cols, fixed)

    wb.save(out)
    print(f"Wrote {out} ({len(wb.sheetnames)} sheets: {', '.join(wb.sheetnames)})")


if __name__ == "__main__":
    main()
