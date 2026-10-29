#!/usr/bin/env python3
"""Keep the LITE Helm chart's copied files equal to their source, byte for byte.

Helm reads nothing outside a chart, so deploy/lite/cloud/helm/tokenlean-lite/files/ holds
copies of the files the chart's ConfigMaps carry. The source stays where it is (the LITE
profile in deploy/lite/config/, the shipped config in config/); this script copies them in.
tests/unit/test_lite_helm_chart.py fails while a copy is stale.

Usage:
    python scripts/sync_lite_chart.py            # copy every stale file
    python scripts/sync_lite_chart.py --check    # report only; exit 1 when one is stale
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART = REPO_ROOT / "deploy" / "lite" / "cloud" / "helm" / "tokenlean-lite"

# Chart path -> source, both relative to the repository root / the chart.
COPIES = {
    "files/agentic.yaml": "deploy/lite/config/agentic.yaml",
    "files/langfuse.yaml": "deploy/lite/config/langfuse.yaml",
    "files/config.yaml.template": "config/config.yaml.template",
    "files/mock_llm.py": "deploy/lite/local/mock/mock_llm.py",
}


def stale() -> list[str]:
    """The chart paths whose bytes differ from their source (or that are missing)."""
    out = []
    for copy, source in COPIES.items():
        target = CHART / copy
        if not target.exists() or target.read_bytes() != (REPO_ROOT / source).read_bytes():
            out.append(copy)
    return out


def crlf_only(copy: str) -> bool:
    """Whether the copy differs from its source only by the source's CRLF line endings."""
    target = CHART / copy
    source = (REPO_ROOT / COPIES[copy]).read_bytes()
    return (target.exists() and b"\r\n" in source
            and source.replace(b"\r\n", b"\n") == target.read_bytes())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report only, change nothing")
    args = parser.parse_args(argv)
    todo = stale()
    for copy, source in COPIES.items():
        state = "STALE" if copy in todo else "ok"
        print(f"{state:5}  {copy}  <-  {source}")
    crlf = [COPIES[copy] for copy in todo if crlf_only(copy)]
    if crlf:
        # Copying would write CRLF into an LF-pinned copy: check the source out again instead.
        for source in crlf:
            print(f"{source} has CRLF line endings (a checkout from before .gitattributes "
                  f"pinned it to LF): delete it, then run: git checkout -- {source}",
                  file=sys.stderr)
        return 1
    if args.check:
        if todo:
            print("run: python scripts/sync_lite_chart.py", file=sys.stderr)
        return 1 if todo else 0
    for copy in todo:
        target = CHART / copy
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO_ROOT / COPIES[copy]).read_bytes())
        print(f"copied {COPIES[copy]} -> {target.relative_to(REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
