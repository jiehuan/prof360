from uw_pipeline.normalize.html_text import html_to_text


def test_blocks_become_lines_and_inline_divs_do_not() -> None:
    html = b"""<html><body>
      <div style="display:none"><ix:header>hidden facts</ix:header></div>
      <p>ITEM 1A.&#160;&#160;Risk Factors</p>
      <div>not stand for <div style="display:inline;">re-election</div> any disag<div style="display:inline">ree</div>ment.</div>
      <table><tr><td>Item 7.</td><td>MD&amp;A</td><td>42</td></tr></table>
      <script>var x = 1;</script>
    </body></html>"""
    assert html_to_text(html).split("\n") == [
        "ITEM 1A. Risk Factors",
        "not stand for re-election any disagreement.",
        "Item 7. MD&A 42",
    ]
