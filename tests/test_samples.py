"""Every bundled synthetic sample must parse, and the set must stay fictional."""

import pytest

from ai_ops_hub.parsing import parse_eml, parse_pasted_text

from conftest import SAMPLES

ALL = sorted(p for p in SAMPLES.iterdir() if p.suffix in {".eml", ".txt"})


def test_ten_samples_present():
    assert len(ALL) == 10


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.name)
def test_sample_parses_with_nonempty_body(path, settings):
    if path.suffix == ".eml":
        m = parse_eml(path.read_bytes(), settings)
    else:
        m = parse_pasted_text(path.read_text(encoding="utf-8"), settings)
    assert m.body_text.strip()


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.name)
def test_sample_addresses_are_reserved_example_domains(path):
    import re

    text = path.read_text(encoding="utf-8")
    for domain in re.findall(r"@([\w.-]+)", text):
        assert domain.lower().endswith(".example"), domain


def test_samples_are_small():
    assert all(p.stat().st_size < 4_000 for p in ALL)
