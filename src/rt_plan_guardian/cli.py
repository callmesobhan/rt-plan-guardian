"""Command-line interface: `rtpg check | rules | rulesets | demo | app`.

Exit codes (stable, for CI and scripts):
  0  PASS, or PASS_WITH_WARNINGS unless --fail-on warning
  1  FAIL (an error-severity rule failed), or warnings with --fail-on warning
  2  usage, input-file or ruleset error
  3  INCOMPLETE (a rule crashed; the plan was NOT fully reviewed)
When a folder holds several cases the worst outcome wins (1 over 3 over 0).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .catalog import catalog_markdown, catalog_table, template_yaml
from .config import builtin_rulesets, load_ruleset
from .engine import ReviewReport, run_review
from .errors import GuardianError
from .loader import group_ignored, load_cases
from .models import Status, Verdict
from .report import FORMATS, display_status, write_reports
from .synthetic import DEFECTS, TECHNIQUES, make_case

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_INCOMPLETE = 0, 1, 2, 3


def _print_summary(report: ReviewReport, *, show_inputs: bool = True) -> None:
    d = report.summary
    ctx = report.context
    techniques = ", ".join(sorted(ctx.techniques)) or "none detected"
    print(f"Case {ctx.case_id}  (technique: {techniques})")
    if show_inputs:
        for f in ctx.files:
            print(f"  {f.kind.upper():9} {f.name}")
        for reason, names in group_ignored(ctx.ignored):
            print(f"  ignored   {len(names)} file(s): {reason}")
        for item in ctx.unassigned:
            print(f"  UNASSIGNED {item.name}: {item.reason}")
    print()
    for r in report.results:
        if r.status in (Status.FAIL, Status.ERROR):
            print(f"{display_status(r.to_dict()):5} {r.rule_id:10} {r.title} - {r.summary}")
            for c in r.failed_checks[:5]:
                print(
                    f"      - {c.subject}: observed {c.observed!r}, expected {c.expected!r}"
                    + (f" ({c.note})" if c.note else "")
                )
            if len(r.failed_checks) > 5:
                print(f"      ... {len(r.failed_checks) - 5} more failed checks")
    for r in report.by_status(Status.SKIPPED):
        print(f"SKIP  {r.rule_id:10} {r.title} - {r.summary}")
    print()
    print(
        f"Verdict: {report.verdict.value}  "
        f"({d['pass']} passed, {d['fail_error']} failed, {d['fail_warning']} warnings, "
        f"{d['skipped']} not evaluated, {d['error']} crashed, {d['not_applicable']} n/a, "
        f"{d['disabled']} disabled)"
    )


def _exit_code(reports: list[ReviewReport], fail_on: str) -> int:
    verdicts = {r.verdict for r in reports}
    if Verdict.FAIL in verdicts:
        return EXIT_FAIL
    if Verdict.PASS_WITH_WARNINGS in verdicts and fail_on == "warning":
        return EXIT_FAIL
    if Verdict.INCOMPLETE in verdicts:
        return EXIT_INCOMPLETE
    return EXIT_OK


def _cmd_check(args: argparse.Namespace) -> int:
    case_set = load_cases(args.paths)
    ruleset = load_ruleset(args.config)
    reports = [run_review(ctx, ruleset) for ctx in case_set.cases]
    many = len(reports) > 1
    if many:
        print(f"Assembled {len(reports)} cases from the supplied files.\n")
    for report in reports:
        if many:
            print("=" * 78)
        _print_summary(report)
        if many:
            print()
    if many:
        print("=" * 78)
        print("Summary:")
        for report in reports:
            print(f"  {report.verdict.value:18} {report.context.case_id}")
    if args.out:
        formats = tuple(f.strip() for f in args.format.split(",") if f.strip())
        out = Path(args.out)
        index = []
        for report in reports:
            target = out / report.context.case_id if many else out
            for path in write_reports(report, target, formats):
                print(f"Report written: {path}")
            index.append(
                {
                    "case_id": report.context.case_id,
                    "verdict": report.verdict.value,
                    "summary": report.summary,
                    "content_sha256": report.content_sha256,
                }
            )
        if many:
            (out / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
            print(f"Index written: {out / 'index.json'}")
    return _exit_code(reports, args.fail_on)


def _cmd_rules(args: argparse.Namespace) -> int:
    print(
        {"md": catalog_markdown, "yaml": template_yaml, "table": catalog_table}[
            args.format
        ]().rstrip("\n")
    )
    return EXIT_OK


def _cmd_rulesets(_: argparse.Namespace) -> int:
    for name, (title, source) in builtin_rulesets().items():
        print(f"{name:28} {title}")
        if source:
            print(f"{'':28} source: {source}")
    return EXIT_OK


def _cmd_demo(args: argparse.Namespace) -> int:
    if args.list:
        width = max(len(n) for n in DEFECTS)
        for name, d in DEFECTS.items():
            print(
                f"{name:<{width}}  [{d.technique}] {d.description}  "
                f"-> fails: {', '.join(sorted(d.expected_failures))}"
            )
        return EXIT_OK
    if not args.outdir:
        print("error: demo needs an output directory (or --list)", file=sys.stderr)
        return EXIT_USAGE
    technique = args.technique
    if args.defect:
        if args.defect not in DEFECTS:
            print(f"error: unknown defect {args.defect!r}; see `rtpg demo --list`", file=sys.stderr)
            return EXIT_USAGE
        technique = DEFECTS[args.defect].technique
    case = make_case(technique=technique)
    if args.defect:
        DEFECTS[args.defect].apply(case)
    for path in case.write(args.outdir):
        print(f"wrote {path}")
    print(f"\nNext: rtpg check {args.outdir} --out {Path(args.outdir) / 'report'}")
    return EXIT_OK


def _cmd_app(_: argparse.Namespace) -> int:
    if importlib.util.find_spec("streamlit") is None:
        print(
            'error: the web app needs Streamlit. Install with: pip install "rt-plan-guardian[app]"',
            file=sys.stderr,
        )
        return EXIT_USAGE
    script = Path(__file__).with_name("app.py")
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(script)])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rtpg",
        description="RT-Plan Guardian: radiotherapy plan consistency & safety checker "
        "(photon, electron, proton/ion, brachytherapy).",
    )
    p.add_argument("--version", action="version", version=f"rtpg {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="review plans (files and/or directories of DICOM-RT objects)")
    c.add_argument("paths", nargs="+", help="RTPLAN / RTSTRUCT / RTDOSE files or directories")
    c.add_argument(
        "--config",
        help="ruleset: a YAML path or a built-in name (see `rtpg rulesets`); default built-in",
    )
    c.add_argument("--out", help="directory to write reports to")
    c.add_argument(
        "--format", default=",".join(FORMATS), help=f"comma list of: {', '.join(FORMATS)}"
    )
    c.add_argument(
        "--fail-on", choices=["error", "warning"], default="error", help="exit 1 on warnings too"
    )
    c.set_defaults(func=_cmd_check)

    r = sub.add_parser("rules", help="list the built-in rules")
    r.add_argument("--format", choices=["table", "md", "yaml"], default="table")
    r.set_defaults(func=_cmd_rules)

    s = sub.add_parser(
        "rulesets", help="list the built-in rulesets and where their limits come from"
    )
    s.set_defaults(func=_cmd_rulesets)

    d = sub.add_parser("demo", help="write a synthetic case (optionally with an injected defect)")
    d.add_argument("outdir", nargs="?")
    d.add_argument(
        "--technique", choices=TECHNIQUES, default="photon", help="synthetic plan technique"
    )
    d.add_argument("--defect", help="name of a defect to inject (see --list)")
    d.add_argument("--list", action="store_true", help="list available defects")
    d.set_defaults(func=_cmd_demo)

    a = sub.add_parser("app", help="launch the Streamlit web app")
    a.set_defaults(func=_cmd_app)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except GuardianError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
