"""Source-level checks: offline assets, no CDN, no os.uname(), no numpy,
and HTML/JS id agreement (catches drift between template and script)."""
import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
STATIC = PROJECT / "static"
TEMPLATE = PROJECT / "templates" / "index.html"
APP_JS = STATIC / "js" / "app.js"
PLOTLY = STATIC / "js" / "plotly.min.js"
STYLE = STATIC / "css" / "style.css"


def test_plotly_is_bundled_locally():
    assert PLOTLY.exists(), "plotly.min.js must be shipped in static/js"
    assert PLOTLY.stat().st_size > 1_000_000, "plotly bundle looks truncated"
    text = PLOTLY.read_text(encoding="utf-8", errors="ignore")
    assert "plotly" in text.lower()


def test_no_runtime_cdn_references():
    for path in (TEMPLATE, APP_JS, STYLE):
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text, f"{path.name} references an insecure URL"
        assert "https://" not in text, f"{path.name} references a CDN/remote URL"
        assert not re.search(r'src=["\']//', text)


def test_no_os_uname_anywhere():
    """The platform correction: platform.uname(), never os.uname()."""
    for path in (PROJECT / "sampler.py", PROJECT / "app.py",
                 PROJECT / "gpu.py", PROJECT / "run_monitor.py"):
        text = path.read_text(encoding="utf-8")
        assert "os.uname(" not in text, f"{path.name} must not use os.uname()"
    assert "platform.uname()" in (PROJECT / "sampler.py").read_text()


def test_no_numpy_dependency():
    for py in PROJECT.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "import numpy" not in text and "from numpy" not in text
    reqs = (PROJECT / "requirements.txt").read_text()
    assert "numpy" not in reqs.lower()


def test_all_js_element_ids_exist_in_template():
    html = TEMPLATE.read_text(encoding="utf-8")
    html_ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
    js = APP_JS.read_text(encoding="utf-8")
    used = set(re.findall(r'getElementById\("([A-Za-z0-9_-]+)"\)', js))
    used |= set(re.findall(r'setText\("([A-Za-z0-9_-]+)"', js))
    # \b so object keys like 'mid: "self"' do not match as 'id: "self"'
    used |= set(re.findall(r'\bid:\s*"([A-Za-z0-9_-]+)"', js))
    missing = sorted(used - html_ids)
    assert not missing, f"ids referenced in app.js missing from index.html: {missing}"


def test_template_has_all_chart_divs():
    html = TEMPLATE.read_text(encoding="utf-8")
    for chart_id in ("cpu-chart", "mem-chart", "net-chart", "disk-chart",
                     "gpu-chart"):
        assert f'id="{chart_id}"' in html


def test_requirements_bounds_real_packages():
    reqs = (PROJECT / "requirements.txt").read_text()
    assert "Flask" in reqs and "psutil" in reqs
    assert "plotly" not in reqs.lower(), "Plotly ships as a static asset, not a pip package"
    assert "fontawesome" not in reqs.lower()
