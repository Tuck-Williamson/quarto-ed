"""
Tests that quarto can render documents to HTML and PDF.
These tests invoke the quarto CLI directly and require quarto to be installed.
R-chunk tests additionally require R + knitr/rmarkdown.
Python-chunk tests additionally require ipykernel in the active Python.
PDF tests require TinyTeX and are skipped otherwise.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

QUARTO_DIR = Path(__file__).parent / "quarto"

# ---------------------------------------------------------------------------
# Availability helpers / marks
# ---------------------------------------------------------------------------

quarto = pytest.mark.skipif(
    shutil.which("quarto") is None,
    reason="quarto not installed",
)


def _r_available() -> bool:
    return shutil.which("Rscript") is not None


def _jupyter_available() -> bool:
    try:
        import ipykernel  # noqa: F401
        import nbclient  # noqa: F401
        import nbformat  # noqa: F401
        return True
    except ImportError:
        return False


def _has_tinytex() -> bool:
    # quarto check latex is not a valid subcommand; check for pdflatex instead,
    # which TinyTeX places on PATH via /root/.TinyTeX/bin/x86_64-linux/pdflatex.
    return shutil.which("pdflatex") is not None


r_available = pytest.mark.skipif(not _r_available(), reason="R (Rscript) not installed")
jupyter_available = pytest.mark.skipif(not _jupyter_available(), reason="ipykernel/yaml not available")


def _quarto_env() -> dict:
    """Env dict that points quarto at the Python interpreter running this test."""
    return {**os.environ, "QUARTO_PYTHON": sys.executable}


def _render(src: Path, dest: Path, *extra_args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["quarto", "render", str(src), "--output-dir", str(dest), *extra_args],
        capture_output=True,
        text=True,
        timeout=120,
        env=_quarto_env(),
    )


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

@quarto
def test_render_html_minimal(tmp_path):
    r = _render(QUARTO_DIR / "minimal.qmd", tmp_path)
    assert r.returncode == 0, r.stderr
    assert any(tmp_path.glob("*.html"))


@quarto
@r_available
def test_render_html_with_r(tmp_path):
    r = _render(QUARTO_DIR / "with_r.qmd", tmp_path)
    assert r.returncode == 0, r.stderr
    html_files = list(tmp_path.glob("*.html"))
    assert html_files, "No HTML output produced"
    content = html_files[0].read_text()
    # knitr should have evaluated the R chunk and embedded the output (2+2=4)
    assert "4" in content


@quarto
@jupyter_available
def test_render_html_with_python(tmp_path):
    r = _render(QUARTO_DIR / "with_python.qmd", tmp_path)
    assert r.returncode == 0, r.stderr
    html_files = list(tmp_path.glob("*.html"))
    assert html_files, "No HTML output produced"
    content = html_files[0].read_text()
    # Jupyter should have run the Python chunk and embedded output (2+2=4)
    assert "4" in content


@quarto
def test_render_html_produces_standalone_file(tmp_path):
    r = _render(QUARTO_DIR / "minimal.qmd", tmp_path, "--self-contained")
    assert r.returncode == 0, r.stderr
    html_files = list(tmp_path.glob("*.html"))
    assert html_files
    # Self-contained HTML embeds all assets — must be non-trivially large
    assert html_files[0].stat().st_size > 1000


# ---------------------------------------------------------------------------
# PDF rendering (requires TinyTeX)
# ---------------------------------------------------------------------------

@quarto
@pytest.mark.skipif(not _has_tinytex(), reason="TinyTeX not available")
def test_render_pdf(tmp_path):
    r = _render(QUARTO_DIR / "to_pdf.qmd", tmp_path)
    assert r.returncode == 0, r.stderr
    assert any(tmp_path.glob("*.pdf"))


@quarto
@r_available
@pytest.mark.skipif(not _has_tinytex(), reason="TinyTeX not available")
def test_render_pdf_with_r(tmp_path):
    r = _render(QUARTO_DIR / "with_r.qmd", tmp_path, "--to", "pdf")
    assert r.returncode == 0, r.stderr
    assert any(tmp_path.glob("*.pdf"))
