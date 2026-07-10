"""
Slow PDF-rendering suite.

Renders a set of Quarto documents to PDF via TinyTeX, each exercising a
different LaTeX path (default template, TOC/numbered sections, tables,
callouts, syntax-highlighted listings). These are deliberately excluded from
normal test runs because each render spins up a full LaTeX toolchain and is
slow.

Gating (see pytest.ini and scripts/run-tests.sh):
  * marked `pdf` → excluded by the default `-m "not pdf"`; run with `-m pdf`
  * `RUN_PDF_TESTS=1` required → explicit opt-in switch
  * TinyTeX (pdflatex) must be on PATH
Locally `scripts/run-tests.sh` runs this in a dedicated parallel container; in
CI it runs only on main-branch (PR-merge) builds.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PDF_DIR = Path(__file__).parent / "quarto" / "pdf"

pytestmark = pytest.mark.pdf


def _has_tinytex() -> bool:
    return shutil.which("pdflatex") is not None


run_pdf = pytest.mark.skipif(
    os.environ.get("RUN_PDF_TESTS") != "1",
    reason="PDF suite disabled (set RUN_PDF_TESTS=1; runs on local + main CI builds)",
)
quarto = pytest.mark.skipif(shutil.which("quarto") is None, reason="quarto not installed")
tinytex = pytest.mark.skipif(not _has_tinytex(), reason="TinyTeX not available")

PDF_DOCS = sorted(PDF_DIR.glob("*.qmd"))


def _render_pdf(src: Path, dest: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["quarto", "render", str(src), "--to", "pdf", "--output-dir", str(dest)],
        capture_output=True,
        text=True,
        timeout=300,
        env=os.environ.copy(),
    )


@quarto
@run_pdf
@tinytex
@pytest.mark.parametrize("doc", PDF_DOCS, ids=lambda p: p.stem)
def test_render_pdf_document(doc: Path, tmp_path):
    result = _render_pdf(doc, tmp_path)
    assert result.returncode == 0, result.stderr
    pdfs = list(tmp_path.glob("*.pdf"))
    assert pdfs, f"No PDF produced for {doc.name}"
    # A real PDF with content is well above a few hundred bytes.
    assert pdfs[0].stat().st_size > 1000, f"PDF for {doc.name} is suspiciously small"


def test_pdf_fixtures_present():
    """Guard against the fixture set silently emptying (which would make the
    parametrized suite vacuously pass). Not gated on TinyTeX — it only checks
    that the .qmd fixtures exist."""
    assert len(PDF_DOCS) >= 5, f"expected >=5 PDF fixtures, found {len(PDF_DOCS)}"
