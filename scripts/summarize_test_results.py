#!/usr/bin/env python3
"""
Print a colour-coded per-test summary table from one or more pytest JUnit XML
files (one per parallel pass). Missing files are treated as empty, so a pass
that produced no XML simply contributes no rows.

Usage:
    python3 scripts/summarize_test_results.py pass1.xml [pass2.xml ...]

Exit codes:
    0 — all tests passed (or only skipped)
    1 — one or more failures
"""

import sys
import os
import xml.etree.ElementTree as ET

# ANSI colour codes
RED_BOLD = "\033[1;31m"
YELLOW   = "\033[1;33m"
GREEN    = "\033[0;32m"
BLINK    = "\033[5;30;41m"   # blinking black on red — sanity-check fallback
RESET    = "\033[0m"


def load(path):
    """Return {nodeid: outcome} from a JUnit XML file produced by pytest."""
    results = {}
    if not os.path.exists(path):
        return results
    for tc in ET.parse(path).iter("testcase"):
        cls  = tc.attrib.get("classname", "")
        name = tc.attrib.get("name", "?")
        key  = f"{cls}::{name}" if cls else name
        if tc.find("failure") is not None or tc.find("error") is not None:
            outcome = "failed"
        elif tc.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        results[key] = outcome
    return results


def fmt(stages):
    return ",".join(str(s) for s in stages) if stages else "-"


def row_color(passes, fails):
    if fails:
        return RED_BOLD
    if not passes:    # skips only — no passes, no failures
        return YELLOW
    if passes:        # passed (± skips), no failures
        return GREEN
    return BLINK      # unexpected state — sanity-check fallback


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} pass1.xml [pass2.xml ...]", file=sys.stderr)
        sys.exit(2)

    # (stage_number, {nodeid: outcome}) for each JUnit XML passed on the CLI.
    stages = [(i, load(path)) for i, path in enumerate(sys.argv[1:], start=1)]

    all_ids = sorted({nid for _, d in stages for nid in d})
    if not all_ids:
        print("No test results found.")
        return

    name_w = max((len(k.split("::")[-1]) for k in all_ids), default=10)
    name_w = max(name_w, len("Test Name"))
    col_w  = 6

    sep = f"+-{'-'*name_w}-+-{'-'*col_w}-+-{'-'*col_w}-+-{'-'*col_w}-+"
    hdr = f"| {'Test Name':<{name_w}} | {'Pass':^{col_w}} | {'Fail':^{col_w}} | {'Skip':^{col_w}} |"

    print(sep)
    print(hdr)
    print(sep)

    totals = {"pass": 0, "fail": 0, "skip_only": 0}
    for nid in all_ids:
        name   = nid.split("::")[-1]
        passes = [s for s, d in stages if d.get(nid) == "passed"]
        fails  = [s for s, d in stages if d.get(nid) == "failed"]
        skips  = [s for s, d in stages if d.get(nid) == "skipped"]

        clr  = row_color(passes, fails)
        line = f"| {name:<{name_w}} | {fmt(passes):^{col_w}} | {fmt(fails):^{col_w}} | {fmt(skips):^{col_w}} |"
        print(f"{clr}{line}{RESET}")

        if fails:
            totals["fail"] += 1
        elif not passes:
            totals["skip_only"] += 1
        else:
            totals["pass"] += 1

    print(sep)
    print(
        f"\nTotal: {len(all_ids)}  "
        f"{GREEN}passed: {totals['pass']}{RESET}  "
        f"{YELLOW}skip-only: {totals['skip_only']}{RESET}  "
        f"{RED_BOLD}failures: {totals['fail']}{RESET}"
    )

    if totals["fail"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
