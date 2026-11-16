"""Anti-drift guard: the docs site must agree with the repo's Markdown.

Every Markdown heading and every fenced code block of the five source
documents must appear in the built HTML in `site/dist/` (one direction only:
the site may add content, it may not lose or alter any). Titles alone would
be theater, so both kinds are compared, as multisets.

The extractors are pure functions over strings; the synthetic tests always
run. The real check needs a built site and runs only with RUN_SITE=1.
"""

import os
import re
import subprocess
from collections import Counter
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# (slug, source path relative to the repo root)
PAGES = [
    ("project-status", "PROJECT_STATUS.md"),
    ("audit", "AUDIT.md"),
    ("onboarding", "docs/ONBOARDING.md"),
    ("live-run", "docs/LIVE_RUN.md"),
    ("decisions", "CLAUDE.md"),
]


def _site_dir():
    return ROOT / "site" / "dist"


# ---------------------------------------------------------------------------
# Markdown side (pure)
# ---------------------------------------------------------------------------

_FENCE_OPEN = re.compile(r"^([ \t]*)(`{3,}|~{3,})(.*)$")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")


def _scan_md(md_text):
    """Yield ('line', text) for non-fence lines, ('fence', info, code) per block."""
    lines = md_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = _FENCE_OPEN.match(line)
        if m and not (m.group(2)[0] == "`" and "`" in m.group(3)):
            indent, fence, info = m.group(1), m.group(2), m.group(3).strip()
            char, length = fence[0], len(fence)
            body = []
            j = i + 1
            while j < n:
                cm = re.match(r"^[ \t]*(" + re.escape(char) + r"{%d,})[ \t]*$" % length, lines[j])
                if cm:
                    break
                body.append(_strip_indent(lines[j], len(indent.expandtabs(4))))
                j += 1
            yield ("fence", info, "\n".join(body))
            i = j + 1  # skip the closing fence (or run off the end if unclosed)
            continue
        yield ("line", line)
        i += 1


def _strip_indent(line, width):
    """Remove up to `width` leading spaces (the opening fence's indentation)."""
    k = 0
    while k < width and k < len(line) and line[k] == " ":
        k += 1
    return line[k:]


def _inline_text(s):
    """Reduce inline Markdown to its visible text."""
    stash = []

    def hold(text):
        stash.append(text)
        return "\x00%d\x00" % (len(stash) - 1)

    s = re.sub(r"(`+)(.+?)\1", lambda m: hold(m.group(2).strip()), s)
    s = re.sub(r"\\([\\`*_{}\[\]()#+\-.!<>|~])", lambda m: hold(m.group(1)), s)
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", s)
    s = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"\1", s)
    s = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", s)
    s = re.sub(r"~~(.+?)~~", r"\1", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], s)


def md_headings(md_text):
    """ATX headings outside fenced code -> [(level, visible text)]."""
    out = []
    for item in _scan_md(md_text):
        if item[0] != "line":
            continue
        m = _ATX.match(item[1])
        if not m:
            continue
        raw = m.group(2) or ""
        raw = re.sub(r"(?:^|[ \t]+)#+[ \t]*$", "", raw)  # closing #s
        out.append((len(m.group(1)), _norm(_inline_text(raw))))
    return out


def md_fences(md_text):
    """Fenced code blocks -> [(info string, code)]."""
    return [(it[1], it[2]) for it in _scan_md(md_text) if it[0] == "fence"]


def _norm(s):
    return " ".join(s.split())


# ---------------------------------------------------------------------------
# HTML side (pure). Adjust here if the real markup differs.
# ---------------------------------------------------------------------------

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "source", "track", "wbr"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# elements inside a code line that are decoration, not code
_SKIP_CLASSES = {"gutter", "ln", "copy", "sr-only"}


def _classes(attrs):
    return set((dict(attrs).get("class") or "").split())


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.headings = []
        self.blocks = []
        self._h_level = None
        self._h_buf = []
        # code
        self._pre_depth = 0
        self._stack = []  # (tag, is_line, is_skip) inside a pre
        self._lines = []
        self._cur = None
        self._raw = []

    def handle_starttag(self, tag, attrs):
        if tag in _HEADING_TAGS and self._pre_depth == 0:
            self._h_level, self._h_buf = int(tag[1]), []
            return
        if tag == "pre":
            if self._pre_depth == 0:
                self._lines, self._raw, self._stack, self._cur = [], [], [], None
            self._pre_depth += 1
            return
        if self._pre_depth:
            if tag == "br":
                self._text("\n")
                return
            if tag in _VOID:
                return
            cl = _classes(attrs)
            is_line = "ec-line" in cl
            is_skip = bool(cl & _SKIP_CLASSES) or dict(attrs).get("aria-hidden") == "true"
            if is_line and self._cur is None:
                self._cur = []
            self._stack.append((tag, is_line, is_skip))

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and self._pre_depth:
            self._text("\n")

    def handle_endtag(self, tag):
        if tag in _HEADING_TAGS and self._h_level == int(tag[1]):
            self.headings.append((self._h_level, _norm("".join(self._h_buf))))
            self._h_level = None
            return
        if tag == "pre" and self._pre_depth:
            self._pre_depth -= 1
            if self._pre_depth == 0:
                if self._lines:
                    self.blocks.append("\n".join(self._lines))
                else:
                    raw = "".join(self._raw)
                    if raw.endswith("\n"):
                        raw = raw[:-1]
                    self.blocks.append(raw)
            return
        if self._pre_depth and tag not in _VOID:
            while self._stack:
                t, is_line, _skip = self._stack.pop()
                if is_line:
                    self._lines.append("".join(self._cur or []))
                    self._cur = None
                if t == tag:
                    break

    def _text(self, data):
        if any(s for (_t, _l, s) in self._stack):
            return
        self._raw.append(data)
        if self._cur is not None:
            self._cur.append(data)

    def handle_data(self, data):
        if self._h_level is not None:
            self._h_buf.append(data)
        if self._pre_depth:
            self._text(data)


def _extract(html_text):
    p = _Extractor()
    p.feed(html_text)
    p.close()
    return p


def html_headings(html_text):
    """Heading elements -> [(level, text inside the element)]."""
    return _extract(html_text).headings


def html_code_blocks(html_text):
    """<pre> blocks -> [code text, lines joined with newline]."""
    return _extract(html_text).blocks


class _Footer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.attrs = None

    def handle_starttag(self, tag, attrs):
        if tag == "footer" and self.attrs is None and "provenance" in _classes(attrs):
            self.attrs = dict(attrs)


def provenance(html_text):
    p = _Footer()
    p.feed(html_text)
    return p.attrs


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def _code_lines(code):
    return tuple(line.rstrip() for line in code.split("\n"))


def drift_problems(md_text, html_text, name):
    """Everything in the Markdown that the HTML lacks (one direction only)."""
    problems = []
    have_h = Counter((lvl, _norm(t)) for lvl, t in html_headings(html_text))
    for lvl, text in md_headings(md_text):
        key = (lvl, _norm(text))
        if have_h[key] > 0:
            have_h[key] -= 1
        else:
            problems.append("%s: heading missing from site: h%d %r" % (name, lvl, text))

    have_c = Counter(_code_lines(c) for c in html_code_blocks(html_text))
    for info, code in md_fences(md_text):
        key = _code_lines(code)
        if have_c[key] > 0:
            have_c[key] -= 1
        else:
            problems.append(
                "%s: code block missing or altered on site (%s): first line %r, %d line(s)"
                % (name, info or "no language", key[0], len(key))
            )
    return problems


# ---------------------------------------------------------------------------
# Synthetic tests (always run)
# ---------------------------------------------------------------------------

SYN_MD = """# Title §1

Intro #hashtag text.

## Setup with `code` and [a link](http://example.test/x) ##

```python
# a shell-ish comment, not a heading
if a < b and c & d:
    print("§")
```

## Duplicate

text

## Duplicate

- item:

  ```
  plain block
    indented line
  ```

~~~sh
echo hi
~~~
"""


def _ec(lines, lang=None):
    rows = "".join(
        '<div class="ec-line"><div class="code"><span>%s</span></div></div>'
        % l.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for l in lines
    )
    la = ' data-language="%s"' % lang if lang else ""
    return "<figure><pre%s><code>%s</code></pre></figure>" % (la, rows)


def _h(level, inner, ident="x"):
    return (
        '<div class="sl-heading-wrapper level-h%d"><h%d id="%s">%s</h%d>'
        '<a class="sl-anchor-link" href="#%s"><span aria-hidden="true">#</span>'
        '<span class="sr-only">Section titled</span></a></div>' % (level, level, ident, inner, level, ident)
    )


SYN_BLOCKS = [
    _ec(["# a shell-ish comment, not a heading", "if a < b and c & d:", '    print("§")'], "python"),
    _ec(["plain block", "  indented line"]),
    _ec(["echo hi"], "sh"),
]
SYN_HEADS = [
    _h(1, "Title §1"),
    _h(2, 'Setup with <code>code</code> and <a href="http://example.test/x">a link</a>'),
    _h(2, "Duplicate"),
    _h(2, "Duplicate"),
]


def _page(heads=SYN_HEADS, blocks=SYN_BLOCKS):
    return "<html><body><main>%s%s</main><footer class=\"provenance\"></footer></body></html>" % (
        "".join(heads),
        "".join(blocks),
    )


def test_synthetic_matching_pair_has_no_problems():
    assert drift_problems(SYN_MD, _page(), "SYN.md") == []


def test_md_extractors_ignore_fenced_hashes_and_non_headings():
    assert md_headings(SYN_MD) == [(1, "Title §1"), (2, "Setup with code and a link"), (2, "Duplicate"), (2, "Duplicate")]
    fences = md_fences(SYN_MD)
    assert [i for i, _ in fences] == ["python", "", "sh"]
    assert fences[0][1].splitlines()[0] == "# a shell-ish comment, not a heading"
    assert fences[1][1] == "plain block\n  indented line"  # list indent stripped
    assert md_headings("#hashtag\n####### seven\n```\n# no\n```\n#\n") == [(1, "")]


def test_md_fence_rules():
    assert md_fences("````\n```\ninner\n```\n````\n") == [("", "```\ninner\n```")]
    assert md_fences("~~~\n```\nx\n~~~\n") == [("", "```\nx")]
    assert md_fences("```\nunclosed\n") == [("", "unclosed\n")]  # unclosed: runs to end of document
    assert md_fences("   ```js\n   a\n    b\n   ```\n") == [("js", "a\n b")]


def test_html_extractors_see_the_structure():
    page = _page()
    assert html_headings(page)[1] == (2, "Setup with code and a link")  # anchor text excluded
    assert html_code_blocks(page)[0].splitlines()[1] == "if a < b and c & d:"
    plain = "<pre>a &lt; b\nc</pre>"
    assert html_code_blocks(plain) == ["a < b\nc"]


def test_missing_heading_is_named():
    heads = [h for h in SYN_HEADS if "Setup" not in h]
    probs = drift_problems(SYN_MD, _page(heads=heads), "SYN.md")
    assert len(probs) == 1 and "SYN.md" in probs[0] and "heading" in probs[0] and "Setup with code and a link" in probs[0]


def test_one_character_change_in_code_is_named():
    blocks = list(SYN_BLOCKS)
    blocks[0] = blocks[0].replace("a &lt; b", "a &lt; c")
    probs = drift_problems(SYN_MD, _page(blocks=blocks), "SYN.md")
    assert len(probs) == 1 and "code block" in probs[0]
    assert "# a shell-ish comment" in probs[0] and "3 line" in probs[0]


def test_trailing_whitespace_is_the_only_tolerance():
    md = "```\na  \n b\n```\n"
    assert drift_problems(md, _page(heads=[], blocks=[_ec(["a", " b"])]), "S.md") == []
    assert drift_problems(md, _page(heads=[], blocks=[_ec(["a", "b"])]), "S.md") != []


def test_headings_without_code_fail():
    probs = drift_problems(SYN_MD, _page(blocks=[]), "SYN.md")
    assert len(probs) == 3 and all("code block" in p for p in probs)


def test_title_only_page_fails():
    probs = drift_problems(SYN_MD, _page(heads=SYN_HEADS[:1], blocks=[]), "SYN.md")
    assert len(probs) == 6  # 3 headings + 3 code blocks


def test_duplicated_heading_present_once_fails():
    heads = SYN_HEADS[:3]  # one "Duplicate" only
    probs = drift_problems(SYN_MD, _page(heads=heads), "SYN.md")
    assert len(probs) == 1 and "Duplicate" in probs[0]


def test_heading_level_must_match():
    heads = list(SYN_HEADS)
    heads[2] = _h(3, "Duplicate")
    probs = drift_problems(SYN_MD, _page(heads=heads), "SYN.md")
    assert len(probs) == 1 and "h2" in probs[0]


def test_extra_html_is_not_a_problem():
    extra = _page(heads=SYN_HEADS + [_h(2, "Only on the site")], blocks=SYN_BLOCKS + [_ec(["extra"])])
    assert drift_problems(SYN_MD, extra, "SYN.md") == []


def test_provenance_parser():
    html = '<footer class="provenance" data-commit="a" data-dirty="false" data-built="t" data-source="S.md"></footer>'
    assert provenance(html)["data-source"] == "S.md"
    assert provenance("<footer></footer>") is None


# ---------------------------------------------------------------------------
# The real check (needs a built site)
# ---------------------------------------------------------------------------

_REMOTE = re.compile(r"^\s*(?:https?:)?//", re.I)
_BANNED = ("fonts.googleapis", "fonts.gstatic", "googletagmanager", "google-analytics", "plausible", "posthog")
_NOT_LOADS = {"canonical", "alternate", "sitemap", "author", "license", "me", "help", "search"}
_TEXT_EXT = {".html", ".css", ".js", ".mjs", ".json", ".xml", ".svg", ".txt", ".webmanifest"}


class _Loads(HTMLParser):
    """Remote resource loads in HTML: script/img/source/iframe src, link href, srcset."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "img", "source", "iframe") and a.get("src"):
            self._chk(tag, "src", a["src"])
        if tag == "link" and a.get("href"):
            rels = set((a.get("rel") or "").lower().split())
            if not rels or not (rels & _NOT_LOADS):
                self._chk(tag, "href", a["href"])
        for key in ("srcset",):
            if tag in ("img", "source") and a.get(key):
                for part in a[key].split(","):
                    self._chk(tag, key, part.strip().split(" ")[0])

    def _chk(self, tag, attr, val):
        if _REMOTE.match(val):
            self.found.append("<%s %s=%r>" % (tag, attr, val))


def _git_head():
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _site_wide_problems(dist):
    probs = []
    for f in sorted(dist.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in _TEXT_EXT:
            continue
        rel = f.relative_to(dist).as_posix()
        text = f.read_text(encoding="utf-8", errors="replace")
        suffix = f.suffix.lower()
        if suffix == ".html":
            p = _Loads()
            p.feed(text)
            probs += ["remote load in %s: %s" % (rel, x) for x in p.found]
        if suffix in (".js", ".mjs"):
            for m in re.finditer(r"""\b(?:src|href)\s*=\s*["']?\s*((?:https?:)?//[^\s"'>]+)""", text, re.I):
                probs.append("remote load in %s: %s" % (rel, m.group(1)[:80]))
        if suffix in (".css", ".html"):
            for m in re.finditer(r"""url\(\s*["']?\s*((?:https?:)?//[^\s"')]+)""", text, re.I):
                probs.append("remote CSS url() in %s: %s" % (rel, m.group(1)[:80]))
            for m in re.finditer(r"""@import\s+(?:url\(\s*)?["']?\s*((?:https?:)?//[^\s"')]+)""", text, re.I):
                probs.append("remote @import in %s: %s" % (rel, m.group(1)[:80]))
        low = text.lower()
        for b in _BANNED:
            if b in low:
                probs.append("banned third-party string %r in %s" % (b, rel))
        if suffix in (".js", ".mjs") and ("document.cookie" in text or re.search(r"set-cookie", text, re.I)):
            probs.append("cookie use in site script %s" % rel)
    return probs


@pytest.mark.skipif(
    os.environ.get("RUN_SITE") != "1",
    reason="RUN_SITE=1 not set: needs a built site (cd site && npm ci && npm run build)",
)
def test_the_built_site_agrees_with_the_repo():
    dist = _site_dir()
    head = _git_head()
    problems = []
    if not dist.is_dir():
        problems.append("site build missing: %s does not exist" % dist)
    else:
        for slug, source in PAGES:
            src_file = ROOT / source
            page = dist / slug / "index.html"
            if not src_file.is_file():
                problems.append("%s: source file missing in repo" % source)
                continue
            if not page.is_file():
                problems.append("%s: built page missing: %s" % (source, page.relative_to(ROOT).as_posix()))
                continue
            md = src_file.read_text(encoding="utf-8")
            html = page.read_text(encoding="utf-8")
            problems += drift_problems(md, html, source)

            fp = provenance(html)
            if fp is None:
                problems.append("%s: provenance footer missing on /%s/" % (source, slug))
                continue
            if fp.get("data-source") != source:
                problems.append("%s: data-source is %r, expected %r" % (source, fp.get("data-source"), source))
            commit = fp.get("data-commit") or ""
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                problems.append("%s: data-commit %r is not 40 lowercase hex" % (source, commit))
            elif not head:
                problems.append("%s: cannot read git HEAD to compare with the build" % source)
            elif commit != head:
                problems.append(
                    "%s: the site was built from %s but HEAD is %s: rebuild" % (source, commit, head)
                )
            built = (fp.get("data-built") or "").strip()
            try:
                datetime.fromisoformat(built.replace("Z", "+00:00"))
            except ValueError:
                problems.append("%s: data-built %r is not ISO-8601" % (source, built))
            if fp.get("data-dirty") not in ("true", "false"):
                problems.append("%s: data-dirty %r is not true/false" % (source, fp.get("data-dirty")))
        problems += _site_wide_problems(dist)

    if problems:
        problems = sorted(set(problems))
        shown = problems[:40]
        msg = "\n".join(shown)
        if len(problems) > 40:
            msg += "\n... and %d more" % (len(problems) - 40)
        pytest.fail("%d site problem(s):\n%s" % (len(problems), msg))
