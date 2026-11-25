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
from .commissioning import (
    MUTATION_CATALOG,
    PROFILES,
    generate_commissioning_suite,
    load_external_results,
    render_commissioning_markdown,
    run_command_adapter_on_suite,
    run_rtpg_on_suite,
    score_commissioning_suite,
    write_commissioning_reports,
)
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


def _cmd_commission_list(_: argparse.Namespace) -> int:
    width = max(len(m) for m in MUTATION_CATALOG)
    for mid, spec in MUTATION_CATALOG.items():
        print(
            f"{mid:<{width}}  [{spec.category:12} | {spec.risk_tier:7} | {spec.subtlety:7}] "
            f"{spec.title}"
        )
    return EXIT_OK


def _cmd_commission_generate(args: argparse.Namespace) -> int:
    mutation_ids = (
        [m.strip() for m in args.mutations.split(",") if m.strip()] if args.mutations else None
    )
    manifest = generate_commissioning_suite(
        args.out,
        source_paths=args.source or None,
        technique=args.technique,
        profile=args.profile,
        mutation_ids=mutation_ids,
        anonymize=not args.keep_phi,
        blind=args.blind,
        seed=args.seed,
    )
    s = manifest["summary"]
    print(f"Generated TG-275 commissioning suite in {args.out}")
    print(
        f"  Cases: {s['total_cases']} ({s['positive_defects']} defects, "
        f"{s['negative_controls']} negative controls, {s['high_risk_defects']} high-risk, "
        f"{s['subtle_defects']} subtle; {s['skipped_inapplicable']} skipped inapplicable)"
    )
    print(f"  Suite SHA-256: {manifest['suite_sha256']}")
    print(f"  Results template: {Path(args.out) / 'results_template.csv'}")
    return EXIT_OK


def _cmd_commission_score(args: argparse.Namespace) -> int:
    if args.self_test:
        observations = run_rtpg_on_suite(args.suite_dir, config=args.config)
        checker_name = args.checker or "RT-Plan Guardian"
        checker_version = args.checker_version or __version__
    elif args.runner_cmd:
        observations = run_command_adapter_on_suite(args.suite_dir, args.runner_cmd)
        checker_name = args.checker or args.runner_cmd.split()[0]
        checker_version = args.checker_version or ""
    elif args.results:
        observations = load_external_results(args.results)
        checker_name = args.checker or "External Plan Checker"
        checker_version = args.checker_version or ""
    else:
        print(
            "error: commission score requires --results, --runner-cmd, or --self-test",
            file=sys.stderr,
        )
        return EXIT_USAGE

    cert = score_commissioning_suite(
        args.suite_dir,
        observations,
        checker_name=checker_name,
        checker_version=checker_version,
        min_sensitivity=args.min_sensitivity,
    )
    print(render_commissioning_markdown(cert))
    if args.out:
        formats = tuple(f.strip() for f in args.format.split(",") if f.strip())
        for path in write_commissioning_reports(cert, args.out, formats=formats):
            print(f"Commissioning certificate written: {path}")

    verdict = cert["verdict"]
    if verdict == "FAIL":
        return EXIT_FAIL
    if verdict == "INCOMPLETE":
        return EXIT_INCOMPLETE
    if verdict == "PASS_WITH_WARNINGS" and args.fail_on == "warning":
        return EXIT_FAIL
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

    cm = sub.add_parser(
        "commission",
        help="vendor-neutral AAPM TG-275 DICOM-RT mutation & plan-checker commissioning harness",
    )
    cm_sub = cm.add_subparsers(dest="commission_command", required=True)

    cm_list = cm_sub.add_parser("list", help="list available commissioning mutations and controls")
    cm_list.set_defaults(func=_cmd_commission_list)

    cm_gen = cm_sub.add_parser(
        "generate",
        help="generate a UID-clean, PHI-stripped TG-275 commissioning suite from any DICOM-RT case or synthetic baseline",
    )
    cm_gen.add_argument(
        "source",
        nargs="*",
        help="optional anonymized clinical DICOM-RT files/folder (omit to use synthetic baseline)",
    )
    cm_gen.add_argument("--out", required=True, help="output directory for the commissioning suite")
    cm_gen.add_argument(
        "--technique",
        choices=[*TECHNIQUES, "all"],
        default="photon",
        help="synthetic baseline technique when no source DICOM paths are supplied",
    )
    cm_gen.add_argument(
        "--profile",
        choices=PROFILES,
        default="all",
        help="mutation profile (all, tg275-core, subtle, controls)",
    )
    cm_gen.add_argument("--mutations", help="comma-separated list of specific mutation ids")
    cm_gen.add_argument(
        "--keep-phi",
        action="store_true",
        help="keep original PatientID/Name tags instead of anonymizing mutants",
    )
    cm_gen.add_argument(
        "--blind",
        action="store_true",
        help="assign neutral CASE_001..CASE_NNN folder names for blinded commissioning",
    )
    cm_gen.add_argument("--seed", type=int, default=0, help="deterministic seed for UID reminting")
    cm_gen.set_defaults(func=_cmd_commission_generate)

    cm_score = cm_sub.add_parser(
        "score",
        help="score any external plan checker (or rtpg via --self-test) and emit a TG-275 certificate",
    )
    cm_score.add_argument("suite_dir", help="suite directory created by `rtpg commission generate`")
    cm_score.add_argument("--results", help="CSV or JSON file with external checker outcomes")
    cm_score.add_argument(
        "--runner-cmd",
        help="CLI command template to run per mutant (e.g. 'my_checker {case_dir}'; exit 0=clean)",
    )
    cm_score.add_argument(
        "--self-test",
        action="store_true",
        help="score RT-Plan Guardian itself against the commissioning suite",
    )
    cm_score.add_argument("--config", help="ruleset for --self-test")
    cm_score.add_argument("--checker", help="display name of the plan checker being commissioned")
    cm_score.add_argument("--checker-version", default="", help="version string of the plan checker")
    cm_score.add_argument("--out", help="directory to write JSON/MD/HTML commissioning certificates")
    cm_score.add_argument("--format", default=",".join(FORMATS), help="comma list of json,md,html")
    cm_score.add_argument(
        "--min-sensitivity",
        type=float,
        default=0.85,
        help="minimum overall sensitivity required for clean PASS (default: 0.85)",
    )
    cm_score.add_argument(
        "--fail-on",
        choices=["error", "warning"],
        default="error",
        help="exit 1 on PASS_WITH_WARNINGS too",
    )
    cm_score.set_defaults(func=_cmd_commission_score)
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
