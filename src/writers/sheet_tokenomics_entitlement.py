from openpyxl.worksheet.worksheet import Worksheet

from src.writers._style import apply_row_style, autofit_columns, write_headers

HEADERS = [
    "Billing Plan Name", "Environment Name", "Capacity Type",
    "Entitled Quantity", "Prepaid Consumed", "Pay-As-You-Go Consumed",
    "Usage Date",
]

_FIELDS = [
    "billing_plan_name", "environment_name", "capacity_type",
    "entitled_quantity", "prepaid_consumed_quantity", "payg_consumed_quantity",
    "usage_date",
]


def write(ws: Worksheet, rows: list[dict]) -> None:
    write_headers(ws, HEADERS)

    if not rows:
        ws.cell(row=2, column=1, value="— No entitlement consumption data imported (set PPADMIN_ENTITLEMENT_CONSUMPTION) —")
    else:
        for i, r in enumerate(rows, start=2):
            for col, field in enumerate(_FIELDS, start=1):
                ws.cell(row=i, column=col, value=r.get(field))
            apply_row_style(ws, i, len(HEADERS))

    autofit_columns(ws, HEADERS)
