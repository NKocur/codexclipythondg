"""Format Markdown for the isolated document viewer."""

STYLE = """
body{font:16px/1.65 system-ui,sans-serif;color:#202838;background:#fff;
max-width:900px;margin:0 auto;padding:32px;overflow-wrap:anywhere}
h1,h2,h3,h4{line-height:1.25;margin-top:1.5em}h1{border-bottom:1px solid #ddd;padding-bottom:.4em}
a{color:#285ac5}pre{background:#f3f5f8;padding:16px;overflow:auto;border-radius:8px}
code{font-family:ui-monospace,monospace;font-size:.9em;background:#f3f5f8;padding:2px 4px}
pre code{padding:0}blockquote{border-left:4px solid #acb7cc;margin-left:0;padding-left:20px;color:#536078}
table{border-collapse:collapse;display:block;overflow:auto}th,td{padding:8px 12px;border:1px solid #d9dee7}
th{background:#f3f5f8}img{max-width:100%}hr{border:0;border-top:1px solid #ddd;margin:24px 0}
"""


def render_markdown(text):
    try:
        from markdown_it import MarkdownIt
    except ImportError as exc:
        raise RuntimeError('Markdown preview needs markdown-it-py. Run: python -m pip install -r requirements-web.txt') from exc
    parser = MarkdownIt('commonmark', {'html': False}).enable('table').enable('strikethrough')
    return '<!doctype html><html><head><meta charset="utf-8"><style>' + STYLE + '</style></head><body>' + parser.render(text) + '</body></html>'
