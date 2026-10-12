# Contributing

## Ground rules

1. **No patient data. Ever.** Not in commits, issues, PR descriptions, screenshots or test fixtures. Reproduce problems with the synthetic builders in `src/rt_plan_guardian/synthetic/`. CI rejects tracked `*.dcm` and report files.
2. **A rule without a proof is not accepted.** Every rule needs a defect in the fault-injection catalogue that makes it fail (and only it, plus any declared cascades), and the clean case must still pass. See [docs/writing-rules.md](docs/writing-rules.md).
3. **Fail safe.** Unknown must never become a pass: use `NotApplicable` / `CannotEvaluate`, don't swallow exceptions, don't guess at unsupported data.
4. Thresholds belong in rule `default_params` / rulesets, not hard-coded. **A numeric clinical limit may only be added to a built-in ruleset together with a `source` citation to an openly readable document you have actually read; say what the document does *not* state.**
5. Real-data findings are welcome as regression tests in `tests/realdata/` (open-access, redistributable datasets only; document licence and citation in `docs/data-sources.md`).

## Workflow

```bash
pip install -e ".[dev,app]"
make fmt          # ruff format + autofix
make check-all    # ruff, mypy, pytest (also verifies docs/rule-catalog.md is current)
make docs         # regenerate docs/rule-catalog.md and the template after changing rules/params
```

## Pull requests

* Describe the *failure mode* the change catches or the behaviour it fixes, not just the code.
* Note any change to rule ids, parameter names or default severities: these are part of the public interface of rulesets and reports. Rule ids are permanent once released.
* Keep the report schema backwards compatible, or bump `REPORT_SCHEMA_VERSION` and say so.

## Reporting a missed or false detection

Open an issue with a synthetic reproduction (a short `synthetic.py` defect or a dataset mutation snippet) and the expected vs actual rule outcomes.
