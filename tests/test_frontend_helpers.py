"""
The escaping helpers in static/common.js that every page relies on to render
statement text safely. Runs common.js under Node with a minimal DOM stub;
skipped where Node isn't installed.
"""
import json
import os
import shutil
import subprocess

import pytest

NODE = shutil.which("node") or (r"C:\Program Files\nodejs\node.exe"
                                if os.path.exists(r"C:\Program Files\nodejs\node.exe") else None)
COMMON_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "src", "static", "common.js")

STUB = """
globalThis.window = { location: { pathname: '/' } };
globalThis.navigator = { platform: 'Win32', userAgent: '' };
globalThis.document = { readyState: 'complete', addEventListener() {}, querySelector() { return null; },
                        body: { insertAdjacentHTML() {} } };
"""


def run_js(expression):
    script = STUB + open(COMMON_JS, encoding="utf-8").read() + f"\nconsole.log(JSON.stringify({expression}));"
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

NASTY = """AT&T <b class="x">'quoted'</b>"""


def test_escape_html_covers_text_and_attributes():
    assert run_js(f"App.escapeHtml({json.dumps(NASTY)})") == \
        "AT&amp;T &lt;b class=&quot;x&quot;&gt;&#39;quoted&#39;&lt;/b&gt;"
    assert run_js("[App.escapeHtml(null), App.escapeHtml(undefined), App.escapeHtml(0)]") == ["", "", "0"]


def test_js_arg_round_trips_through_an_inline_handler():
    # What the browser does: HTML-decode the attribute, then parse it as JS.
    decoded = run_js(f"""App.jsArg({json.dumps(NASTY)})
        .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>').replace(/&amp;/g, '&')""")
    assert json.loads(decoded) == NASTY


def test_formatters():
    assert run_js("[App.monthLabel('2024-07'), App.fmtDate('2024-07-05'), App.compactMoney(120000), App.compactMoney(25000000), App.money(1234567)]") == \
        ["Jul 24", "5 Jul 2024", "₹1.2L", "₹2.5Cr", "₹12,34,567"]
