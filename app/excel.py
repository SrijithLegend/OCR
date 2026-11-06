"""Excel export built from the CURRENT (edited) job state. Returns bytes."""
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .constants import CASE_TITLES, CASES
from .schemas import Job
from .textutil import norm, to_num

HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(bold=True, color="FFFFFF")
RED, YELLOW = PatternFill("solid", fgColor="FFC7CE"), PatternFill("solid", fgColor="FFEB9C")
BLUE, GREEN = PatternFill("solid", fgColor="BDD7EE"), PatternFill("solid", fgColor="C6EFCE")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
SEV = {"error": 2, "warning": 1, "info": 0}


def put(ws, row, col, value):
    """Write a cell. Numbers become numbers; any other string is forced to text so a value
    like =HYPERLINK(...) typed into the UI can never execute as a formula."""
    c = ws.cell(row, col)
    if isinstance(value, str):
        n = to_num(value)
        if n is not None:
            c.value = n
        else:
            c.value = value
            if value[:1] in ("=", "+", "-", "@"):
                c.data_type = "s"
    else:
        c.value = value
    return c


def _style(ws, widths):
    for c in ws[1]:
        c.fill, c.font = HDR_FILL, HDR_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for row in ws.iter_rows(min_row=1):
        for c in row:
            c.border = BORDER
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def cell_fill(c):
    """Human state wins over model flags: edited (blue) > reviewed (green) > error > warning."""
    if c.edited:
        return BLUE
    if c.reviewed:
        return GREEN
    s = max((SEV[f.severity] for f in c.flags), default=-1)
    return RED if s == 2 else YELLOW if s == 1 else None


def build_xlsx(job: Job) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Process Conditions"
    head = ["Source File", "Tag", "Section", "Row #", "Parameter", "Kind", "Unit"] \
        + [CASE_TITLES[c] for c in CASES] + ["Page", "Status"]
    for j, h in enumerate(head, 1):
        ws.cell(1, j, h)
    r = 1
    for sh in job.sheets:
        groups = {}
        for c in sh.cells:
            groups.setdefault((norm(c.section), norm(c.parameter), c.kind), []).append(c)
        for cells in groups.values():
            r += 1
            first = cells[0]
            units = []
            for c in cells:
                if c.unit and c.unit not in units:
                    units.append(c.unit)
            for j, v in enumerate([sh.file_name, sh.tag or sh.title or "", first.section or "", first.row_no,
                                   first.parameter, first.kind, " / ".join(units) or None], 1):
                put(ws, r, j, v)
            status = ""
            for c in cells:
                j = 8 + CASES.index(c.case)
                put(ws, r, j, c.value)
                f = cell_fill(c)
                if f:
                    ws.cell(r, j).fill = f
                if not c.reviewed and not c.edited and any(x.severity == "error" for x in c.flags):
                    status = "ERROR"
                elif not c.reviewed and not c.edited and any(x.severity == "warning" for x in c.flags) \
                        and status != "ERROR":
                    status = "CHECK"
            if not status and any(c.edited for c in cells):
                status = "EDITED"
            put(ws, r, 8 + len(CASES), first.page)
            put(ws, r, 9 + len(CASES), status)
            if status in ("ERROR", "CHECK"):
                ws.cell(r, 9 + len(CASES)).fill = RED if status == "ERROR" else YELLOW
    _style(ws, [20, 16, 22, 7, 38, 11, 14] + [14] * len(CASES) + [6, 9])

    w2 = wb.create_sheet("Sheet Info")
    for j, h in enumerate(["Source File", "Tag", "Title", "Item", "Service", "Fluid", "Line No",
                           "Project", "Template / Vendor", "Pages", "Has Values", "Cells"], 1):
        w2.cell(1, j, h)
    for n, sh in enumerate(job.sheets, 2):
        row = [sh.file_name, sh.tag, sh.title, sh.info.get("item_no"), sh.service, sh.fluid,
               sh.info.get("line_no"), sh.info.get("project"), sh.info.get("format_name"),
               ", ".join(map(str, sh.pages)), "yes" if sh.has_values else "no", len(sh.cells)]
        for j, v in enumerate(row, 1):
            put(w2, n, j, v)
    _style(w2, [20, 16, 28, 8, 24, 16, 14, 18, 22, 10, 10, 8])

    w3 = wb.create_sheet("Long Format")
    for j, h in enumerate(["Source File", "Tag", "Section", "Row #", "Parameter", "Canonical", "Kind",
                           "Case", "Value", "Unit", "Page"], 1):
        w3.cell(1, j, h)
    n = 1
    for sh in job.sheets:
        for c in sh.cells:
            n += 1
            for j, v in enumerate([sh.file_name, sh.tag or sh.title, c.section, c.row_no, c.parameter,
                                   c.canonical, c.kind, c.case, c.value, c.unit, c.page], 1):
                put(w3, n, j, v)
    _style(w3, [20, 16, 22, 7, 38, 20, 11, 10, 16, 12, 6])

    w4 = wb.create_sheet("Review & Edits")
    for j, h in enumerate(["Type", "Severity", "Source File", "Tag", "Parameter", "Column", "Detail"], 1):
        w4.cell(1, j, h)
    n = 1
    for sh in job.sheets:
        for f in sh.notes:
            n += 1
            for j, v in enumerate(["Sheet flag", f.severity.upper(), sh.file_name, sh.tag or sh.title,
                                   "", "", f.message], 1):
                put(w4, n, j, v)
        for c in sh.cells:
            if c.edited:
                n += 1
                for j, v in enumerate(["Human edit", "", sh.file_name, sh.tag or sh.title, c.parameter,
                                       c.case, f"{c.original_value!r} {c.original_unit or ''} -> "
                                       f"{c.value!r} {c.unit or ''}"], 1):
                    put(w4, n, j, v)
            for f in c.flags:
                n += 1
                state = " [resolved by reviewer]" if (c.reviewed or c.edited) else ""
                for j, v in enumerate(["Cell flag", f.severity.upper(), sh.file_name, sh.tag or sh.title,
                                       c.parameter, c.case, f.message + state], 1):
                    put(w4, n, j, v)
    if n == 1:
        w4.append(["", "", "", "", "", "", "Nothing flagged (still spot-check against the source)."])
    _style(w4, [12, 10, 20, 16, 34, 10, 90])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
