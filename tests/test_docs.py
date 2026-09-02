# tests/test_docs.py
#
# Hygiene checks on the public docs: no references to internal files, and
# README links that survive PyPI's rendering.
#
# PyPI freezes each release's rendered README at build time and resolves
# relative links against pypi.org, so a leak or a relative link there is
# permanent for that release. These tests make either one a failing build
# instead of something a reader finds on the live page.

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DOCS = ["README.md", "USAGE.md", "ARCHITECTURE.md", "CHANGELOG.md",
               "CLA.md", "MANIFEST.md"]

# Internal-only paths and files that must never appear in public text.
# pypi.org/project/... URLs are the one legitimate "project/" spelling.
_INTERNAL = re.compile(
    r"(?<!pypi\.org/)project/|CODING\.md|ROADMAP|BUGS\.md|TESTS\.md|PRD"
    r"|~/\.claude|\.claude/"
)

# Everything that ships in the wheel: the package's Python plus the viewer's
# static frontend, which is public text the same way the docs are.
_SHIPPED_GLOBS = ["*.py", "*.html", "*.js", "*.css"]


@pytest.mark.parametrize("doc", PUBLIC_DOCS)
def test_public_docs_reference_no_internal_files(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    hits = [
        (i, line)
        for i, line in enumerate(text.splitlines(), 1)
        if _INTERNAL.search(line)
    ]
    assert not hits, (
        f"{doc} references an internal file or folder; state the behavior "
        f"directly instead: {hits}"
    )


def test_shipped_source_references_no_internal_files():
    for pattern in _SHIPPED_GLOBS:
        for path in sorted((ROOT / "traceact").rglob(pattern)):
            hits = [
                (i, line)
                for i, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1
                )
                if _INTERNAL.search(line)
            ]
            assert not hits, (
                f"{path.relative_to(ROOT)} ships in the wheel and references "
                f"an internal file: {hits}"
            )


def test_readme_links_are_absolute_for_pypi():
    # PyPI resolves relative links against pypi.org, silently breaking
    # them; every markdown link in the README must be a full URL.
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\]\(([^)]+)\)", text)
    relative = [
        link
        for link in links
        if not link.startswith(("http://", "https://", "#", "mailto:"))
    ]
    assert not relative, f"relative links break on PyPI's rendered page: {relative}"
