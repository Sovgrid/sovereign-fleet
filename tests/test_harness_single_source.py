"""Facts about a harness live on its class in fleet/plugins/harness_base.py, nowhere else.

On 2026-10-26 two hand-kept tables (harness -> binary in the router and the roster), a hand list of
own-login CLIs, a hand list in the grid backend and the dashboard's harness menu all missed the new
"cursor" harness; the executor failed its first task with KeyError: 'cursor'. Everything now derives
from ALL_HARNESSES. This test fails when a new hand list appears: a line outside harness_base that
names two or more built-in harnesses as string literals.
"""

import re
from pathlib import Path

from fleet.core.config import harness_binary, own_login_harnesses
from fleet.plugins.harness_base import ALL_HARNESSES

SRC = Path(__file__).resolve().parents[1] / "src" / "fleet"
NAMES = sorted({cls.name for cls in ALL_HARNESSES}, key=len, reverse=True)
_LITERAL = re.compile(r"""["'](%s)["']""" % "|".join(re.escape(n) for n in NAMES))


def _hand_lists():
    hits = []
    for path in [*SRC.rglob("*.py"), *SRC.rglob("*.html"), *SRC.rglob("*.js")]:
        if path.name == "harness_base.py":
            continue
        for no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if line.lstrip().startswith("tags:"):   # help-page search words, not a harness list
                continue
            if len(set(_LITERAL.findall(line))) >= 2:
                hits.append(f"{path.relative_to(SRC)}:{no}: {line.strip()[:120]}")
    return hits


def test_no_hand_kept_list_of_harnesses_outside_their_classes():
    hits = _hand_lists()
    assert not hits, ("derive from ALL_HARNESSES (config.builtin_harnesses / harness_binary / "
                      "own_login_harnesses) instead of listing harnesses:\n" + "\n".join(hits))


def test_derived_facts_cover_every_harness():
    for cls in ALL_HARNESSES:
        assert harness_binary(cls.name) == cls.binary
        assert cls.label, f"{cls.name} needs a label for the dashboard"
    assert set(own_login_harnesses()) == {c.name for c in ALL_HARNESSES if getattr(c, "own_login", False)}
    assert {"claude-code", "agy", "cursor"} <= set(own_login_harnesses())
