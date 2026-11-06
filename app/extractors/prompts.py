"""Prompts and tool schemas for the Claude vision extractor (verbatim from the tested CLI script)."""
from ..constants import CANON, CASES, KINDS

SYSTEM = """You transcribe the SERVICE / PROCESS CONDITIONS of industrial datasheets from many customers and vendors. Layouts differ; the job does not.

SCOPE - extract only:
- Sheet identity: title, tag / item / KKS number, service, fluid, line number, project, template or vendor name.
- The block(s) describing the duty of the equipment, under headings such as PROCESS DATA, OPERATING DATA, OPERATING CONDITIONS, SERVICE CONDITIONS, UPSTREAM / DOWNSTREAM CONDITIONS, DESIGN CONDITIONS, or "Process Data" on relief-sheets. Include the fluid name / state and critical or shut-off pressure when printed with that block.
- IGNORE: pipe line, body, bonnet, trim, actuator, positioner, accessories, testing, notes, revision/approval tables, logos, charts.

OUTPUT: one entry per FILLED cell. Fields:
- section: block heading as printed (or null).
- row_no: printed line number at the left of the row, else null.
- parameter: row label EXACTLY as printed, symbols included, e.g. "Inlet Pressure (P1)".
- canonical: closest value from the enum. Use "other" if nothing fits; never force a fit. Flow: mass units (kg/h, lb/h) -> flow_rate_mass; volumetric liquid (m3/h, gpm) -> flow_rate_volume; gas at standard conditions (scfh, Nm3/h) -> flow_rate_gas_std. Upstream/inlet temperature T1 -> inlet_temperature; T2 -> outlet_temperature. dP / pressure differential / pressure drop -> pressure_drop. Cp/Cv, gamma, isentropic exponent -> specific_heat_ratio.
- kind: "condition" = specified duty value (flow, pressures, temperatures, fluid properties); "design" = design limits (design pressure/temperature, shut-off or max allowable differential pressure, corrosion allowance); "calculated" = results of sizing sitting in the same table (Cv, % open / travel, capacity required, noise / SPL, velocities, Reynolds number).
- case: which column the value is under. Map by the printed HEADER TEXT, never by position (some sheets print Max/Norm/Min, others Min/Normal/Max): max/maximum -> maximum; norm/normal/operating -> normal; min/minimum -> minimum; design -> design; "Others"/"Other"/"Notes" -> other; a single value column with no case header -> single.
- case_label: the column header exactly as printed (or null).
- value: the cell text EXACTLY as printed.
- unit: unit exactly as printed, taken from the Units column or from inside the label (e.g. "Flow Rate Mass kg/h"). Keep gauge/absolute markers: "bar(g)", "kPaa", "kg/cm2g". Write superscripts as plain digits. If one unit applies to the whole row, repeat it on every entry of the row. null if none.
- page: page number the cell is on.

TRANSCRIPTION RULES (accuracy matters more than completeness):
1. Copy values exactly: digits, decimals, exponents (1.4E+06), signs, "<", ">". Never round, convert units, compute, reorder or normalise.
2. Never infer, interpolate or copy a value from another cell or another sheet. Empty cell -> no entry.
3. If any character is not clearly legible, set value to "UNREADABLE". Do not guess digits.
4. Sub-headers such as "Liquid / Liquid / Liquid" under Max / Norm / Min are VALUES of a row (e.g. Inlet State), not case labels.
5. Watermarks, stamps, logos or revision clouds overlapping text are not data. Read through them; if a character stays unclear use "UNREADABLE". Black bars / redactions = empty cell.
6. Placeholder text such as "XXXX" or "xxxxxxxx" = empty. "N/A", "NA", "-", "NIL" are real answers: copy them.
7. A blank template (no values filled in) -> has_values=false and entries=[]. Do NOT invent example values.
8. A sheet's title or tag can be on a different page from its table. Attach each table to the correct sheet using the tag/title, not page order; list the pages used in source_pages. One record per sheet.
9. Sheets without a process/service-conditions block (or with nothing filled) still get a record with has_values=false.
10. TEXT LAYER (when supplied) is real text: use it for characters; the images decide which row/column a value belongs to. If they disagree on a digit, use "UNREADABLE". ZOOM tiles are overlapping crops of the same page for legibility; emit each cell once.

Return the result only through the record_service_conditions tool."""

ADJ_SYSTEM = """You re-read specific cells of datasheets. For each numbered question, locate the cell using the page, section, row label and column header given, and report the value and unit EXACTLY as printed.
- Do not infer from neighbouring cells, other columns or physics.
- If the cell is empty, return value null. If any character is not clearly legible, return "UNREADABLE".
- A TEXT LAYER, when supplied, holds real characters; the images decide the row/column.
Return answers only through the answer_cells tool."""

STR = {"type": ["string", "null"]}
ENTRY = {
    "type": "object",
    "properties": {
        "section": STR,
        "row_no": {"type": ["integer", "null"]},
        "parameter": {"type": "string"},
        "canonical": {"type": "string", "enum": CANON},
        "kind": {"type": "string", "enum": KINDS},
        "case": {"type": "string", "enum": CASES},
        "case_label": STR,
        "value": STR,
        "unit": STR,
        "page": {"type": "integer"},
    },
    "required": ["parameter", "canonical", "kind", "case", "value", "unit", "page"],
}
RECORD_TOOL = {
    "name": "record_service_conditions",
    "description": "Record the service/process conditions of every sheet found.",
    "input_schema": {
        "type": "object",
        "properties": {
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sheet_title": STR, "format_name": STR, "tag": STR, "item_no": STR,
                        "service": STR, "fluid": STR, "line_no": STR, "project": STR,
                        "source_pages": {"type": "array", "items": {"type": "integer"}},
                        "has_values": {"type": "boolean"},
                        "entries": {"type": "array", "items": ENTRY},
                    },
                    "required": ["tag", "source_pages", "has_values", "entries"],
                },
            }
        },
        "required": ["records"],
    },
}
ANSWER_TOOL = {
    "name": "answer_cells",
    "description": "Report the re-read value of each requested cell.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}, "value": STR, "unit": STR},
                    "required": ["id", "value", "unit"],
                },
            }
        },
        "required": ["answers"],
    },
}
