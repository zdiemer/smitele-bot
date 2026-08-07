"""The layout container is styled by the name the app actually renders.

This exists because it did not, for three releases. `App.tsx` rendered
`<div className="shell">` while the stylesheet defined `.sheet` — so
`max-width`, the centring margin and every bit of horizontal padding silently
did nothing, and the page ran edge to edge.

What made it survive review is worth naming: the token *was* in the served CSS,
so every check of the form "is `--measure: 50rem` in the bundle?" passed while
the page kept ignoring it. A stylesheet being delivered is not a stylesheet
being applied, and tuning the value three times could never have fixed a
selector that matched nothing.

This is a source-level check on purpose — it needs no browser, so it runs in the
same suite as everything else rather than in a CI job nobody has set up.
"""

from __future__ import annotations

import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(HERE, "..", "src", "web", "ui", "src")

pytestmark = pytest.mark.skipif(
    not os.path.isdir(UI), reason="web UI sources not present"
)


def read(*parts: str) -> str:
    with open(os.path.join(UI, *parts), "r", encoding="utf-8") as handle:
        return handle.read()


def css() -> str:
    return read("styles.css")


def class_names_in(source: str) -> set:
    """Every *statically known* class name in a TSX file.

    Interpolated regions are cut out before splitting, so
    `` `mark mark-${health}` `` contributes `mark` and not the stub `mark-`.
    Anything the file computes at runtime is deliberately out of scope — this
    check is for names that are visibly constant and therefore visibly wrong
    when no rule matches them.
    """
    found = set()
    for match in re.finditer(r'className=(?:"([^"]*)"|\{`([^`]*)`\})', source):
        literal = match.group(1) or match.group(2)
        literal = re.sub(r"\$\{[^}]*\}", " ", literal)
        for token in literal.split():
            if re.fullmatch(r"[a-zA-Z][\w-]*[a-zA-Z0-9]", token):
                found.add(token)
    return found


def defined_classes() -> set:
    return set(re.findall(r"\.([a-zA-Z][\w-]*)", css()))


def test_the_app_root_is_styled():
    """The exact failure: a container class with no rule behind it."""
    root = re.search(r'<div className="([\w-]+)">', read("App.tsx"))

    assert root, "App.tsx no longer opens with a literal container class"
    assert root.group(1) in defined_classes(), (
        f'App.tsx renders <div class="{root.group(1)}"> but styles.css defines no '
        f"rule for it — max-width, centring and padding will silently do nothing"
    )


def test_the_container_actually_constrains_and_pads():
    """A rule that exists but sets neither is the same bug wearing a hat."""
    block = re.search(r"\.shell\s*\{([^}]*)\}", css())

    assert block, "no .shell rule"
    body = block.group(1)
    assert "max-width" in body, ".shell must cap the measure"
    assert "margin" in body and "auto" in body, ".shell must centre"
    assert "padding" in body, ".shell must pad — this is the reported bug"


@pytest.mark.parametrize(
    "view",
    ["App.tsx", "components.tsx", "charts.tsx"],
)
def test_every_class_a_view_renders_has_a_rule(view):
    """Catches the same mistake anywhere else it is made.

    Scoped to the files that own layout rather than every view, because a
    one-off utility class is a judgement call and a missing *container* is not.
    """
    defined = defined_classes()
    used = class_names_in(read(view))
    missing = sorted(name for name in used if name not in defined)

    assert not missing, f"{view} renders classes with no CSS rule: {missing}"
