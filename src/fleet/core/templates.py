"""Task templates as files (ARCHITECTURE.md, "Templates are MCP prompts").

Three places, a later one overriding an earlier one by id (the file stem):

1. built-in: ``fleet/templates/*.md`` in the package;
2. global: ``<fleet root>/templates/*.md``;
3. per project: ``<project path>/.fleet/templates/*.md``.

A file is a front-matter block between ``---`` lines, then the body::

    ---
    name: Fix a bug
    description: A failing behaviour, fixed with a test
    blank.where: Where | src/upload
    blank.what: What goes wrong | uploads fail on a slow link
    ---
    Fix the bug in {where}: {what}. Add a test that fails without the fix.

A template body is untrusted text: it is shown as text and sent as a task prompt, never run or
rendered as markup. Files that are too large, symlinked, outside their directory or malformed are
skipped with a logged reason; one bad file never hides the others.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("fleet.core.templates")

BUILTIN_DIR = Path(__file__).resolve().parents[1] / "templates"
MAX_BYTES = 16 * 1024
MAX_FILES = 100
ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")
BLANK_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_FIELD_MAX = 200


class TemplateError(ValueError):
    pass


def parse_template(tid: str, raw: str) -> Dict[str, Any]:
    """One template from its file text; raises TemplateError with the reason."""
    if not ID_RE.match(tid):
        raise TemplateError(f"id {tid!r} must match [a-z0-9-]{{1,64}}")
    lines = raw.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise TemplateError("no front matter (the file must start with a '---' line)")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise TemplateError("front matter is not closed with a '---' line") from None
    meta: Dict[str, str] = {}
    blanks: List[Dict[str, str]] = []
    for no, line in enumerate(lines[1:end], 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            raise TemplateError(f"line {no}: expected 'key: value'")
        if len(value) > _FIELD_MAX:
            raise TemplateError(f"line {no}: value longer than {_FIELD_MAX} characters")
        if key.startswith("blank."):
            bid = key[len("blank."):]
            if not BLANK_RE.match(bid):
                raise TemplateError(f"line {no}: blank id {bid!r} must match [a-z][a-z0-9_]{{0,31}}")
            if any(b["id"] == bid for b in blanks):
                raise TemplateError(f"line {no}: blank {bid!r} twice")
            label, _, placeholder = value.partition("|")
            blanks.append({"id": bid, "label": label.strip() or bid, "placeholder": placeholder.strip()})
        elif key in ("name", "description"):
            meta[key] = value
        else:
            raise TemplateError(f"line {no}: unknown key {key!r} (name, description, blank.<id>)")
    if not meta.get("name"):
        raise TemplateError("front matter has no name")
    text = "\n".join(lines[end + 1:]).strip()
    if not text:
        raise TemplateError("the body is empty")
    used = set(re.findall(r"\{([a-z][a-z0-9_]{0,31})\}", text))
    missing = sorted(used - {b["id"] for b in blanks})
    if missing:
        raise TemplateError(f"the body uses {', '.join('{' + m + '}' for m in missing)} without a blank line for it")
    return {"id": tid, "name": meta["name"], "description": meta.get("description", ""),
            "blanks": blanks, "text": text}


def _read_dir(directory: Path, scope: str) -> List[Dict[str, Any]]:
    if not directory.is_dir() or directory.is_symlink():
        return []
    base = directory.resolve()
    out: List[Dict[str, Any]] = []
    files = sorted(p for p in directory.iterdir() if p.suffix == ".md")
    if len(files) > MAX_FILES:
        logger.warning("templates in %s: %d files, only the first %d are read", directory, len(files), MAX_FILES)
        files = files[:MAX_FILES]
    for path in files:
        why = _unsafe(path, base)
        if why:
            logger.warning("template %s skipped: %s", path, why)
            continue
        try:
            tpl = parse_template(path.stem, path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, TemplateError) as e:
            logger.warning("template %s skipped: %s", path, e)
            continue
        out.append({**tpl, "scope": scope})
    return out


def _unsafe(path: Path, base: Path) -> Optional[str]:
    if path.is_symlink():
        return "a symlink"
    try:
        real = path.resolve()
        real.relative_to(base)
    except (OSError, ValueError):
        return "resolves outside its directory"
    if not real.is_file():
        return "not a regular file"
    if real.stat().st_size > MAX_BYTES:
        return f"larger than {MAX_BYTES // 1024} KiB"
    return None


def template_dirs(cfg: Any, project: Optional[str] = None) -> List[Tuple[Path, str]]:
    """The directories in override order, with their scope name."""
    dirs: List[Tuple[Path, str]] = [(BUILTIN_DIR, "built-in")]
    root = getattr(cfg, "root", None)
    if root:
        dirs.append((Path(root) / "templates", "global"))
    if project:
        proj = (getattr(cfg, "projects", None) or {}).get(project)
        if proj is None:
            raise TemplateError(f"unknown project {project!r}")
        path = getattr(proj, "path", "")
        if path:
            dirs.append((Path(path) / ".fleet" / "templates", "project"))
    return dirs


def load_templates(cfg: Any, project: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every template for *project* (or the global ones), later sources overriding earlier by id."""
    by_id: Dict[str, Dict[str, Any]] = {}
    for directory, scope in template_dirs(cfg, project):
        for tpl in _read_dir(directory, scope):
            by_id[tpl["id"]] = tpl
    return sorted(by_id.values(), key=lambda t: (t["scope"] != "project", t["scope"] != "global", t["name"].lower()))


def fill(tpl: Dict[str, Any], values: Dict[str, Any]) -> str:
    """The body with each {blank} replaced by its value, or its placeholder when none is given."""
    text = tpl["text"]
    for b in tpl["blanks"]:
        v = values.get(b["id"])
        text = text.replace("{" + b["id"] + "}", str(v) if v not in (None, "") else b["placeholder"])
    return text
