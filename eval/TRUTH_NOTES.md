# How `eval/truth.csv` was made

- **Annotator: Claude (an AI), not a human.** CLAUDE.md rule 6 asks for a human to read the documents; the repo owner
  instead asked Claude to do it. Values were read from rendered page images of `eval/sheets/tender_p15..p20.pdf`
  (130 dpi) and typed into `eval/build_truth.py`, not copied from workbooks. `prefilled=0` everywhere.
  Nobody has double-checked them. Re-key or spot-check a sample before quoting the numbers externally.
- Row keys (valve, section, parameter, case) start from the union of cells both passes produced
  (`eval/make_template.py`), plus 15 rows added for printed cells the tool never produced. Template rows with no
  printed cell have an empty value; a tool cell there counts as spurious.
- Choices made by the annotator that affect scores:
  - Single-valued cells (Fluid, State, Cv, Inlet/Outlet velocity, specific gravity...) use case `Single`
    (or `STREAM-n` on 2-stream sheets). A tool that files them under Minimum/Normal is scored wrong.
  - Spelling used for the delta-P rows is `dPressure Shut Off` (printed as a delta sign).
  - Units: taken from the cell or row label when printed, else from the sheet header (e.g. `Kg/cm2g`, `°C`, `MT/D`).
    Text/`*` cells have no unit checked. Because many header-only units are blank in the tool output, results report
    both `cell_accuracy` (unit must match) and `cell_accuracy_ignoring_unit`.
- The unit policy was fixed before looking at per-cell errors but after seeing aggregate test scores once;
  the extra metric was added after seeing that unit mismatches dominate the errors.
