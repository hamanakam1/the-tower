"""The dashboard is a single HTML file that renders API data with `innerHTML`.

Node names, task text and event messages are operator input. They are shown
in the same origin as the endpoints that can arm a node and kill a flight, so
an unescaped interpolation is not a cosmetic bug.

These tests are static: they read the template literals and fail on a raw
interpolation of a field that came from the API.
"""
import re
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "the_tower" / "static" / "index.html"

# Fields populated from API payloads. Numbers (cpu_percent, temperature_c, …)
# cannot carry markup and are deliberately not listed.
UNTRUSTED = (
    "task", "callsign", "message", "reason", "container_id", "node_id",
    "name", "role", "location", "specs", "constraints", "kind", "severity",
    "status", "ladder", "source", "node_name", "flight_id",
)


@pytest.fixture(scope="module")
def script() -> str:
    html = DASHBOARD.read_text()
    start = html.index("<script>")
    end = html.rindex("</script>")
    return html[start:end]


def _interpolations(script: str):
    """Yield the body of each `${ … }` in the dashboard script."""
    return re.findall(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", script)


def _references_untrusted(expr: str) -> bool:
    return any(re.search(rf"\.{field}\b", expr) for field in UNTRUSTED)


def test_esc_is_defined_and_escapes_the_dangerous_characters(script):
    assert "function esc(" in script
    for ch in ("&", "<", ">", '"', "'"):
        assert ch in script.split("function esc(")[1].split("}")[0], ch


def test_no_api_field_is_interpolated_without_escaping(script):
    unsafe = []
    for expr in _interpolations(script):
        if "esc(" in expr:
            continue
        if not _references_untrusted(expr):
            continue
        # a comparison or ternary produces a literal, not the field itself
        if any(op in expr for op in ("===", "!==", "? ")):
            continue
        unsafe.append(expr.strip())
    assert not unsafe, "unescaped API field(s) rendered into innerHTML: " + repr(unsafe)


def test_the_enforcement_button_does_not_carry_data_in_an_inline_handler(script):
    """`onclick="f('${n.spec.id}')"` is a second injection path: escaping the
    text does not help inside an HTML attribute holding JS."""
    assert "onclick=\"setEnforcement(" not in script
    assert "addEventListener(\"click\"" in script
    assert "data-node=" in script
