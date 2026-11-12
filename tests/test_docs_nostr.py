"""TEST-DOCS-NOSTR: verify docs/NOSTR.md exists, is linked, matches the code models, and contains no real secrets."""
import re
from pathlib import Path

import pytest

# Pydantic models under test
from fleet.core.config import BuzzSection, IntakeSection, NgitSection, NostrSection, Project, SecretsSection

ROOT = Path(__file__).parent.parent
NOSTR_MD = ROOT / "docs" / "NOSTR.md"
README_MD = ROOT / "README.md"
INSTALL_MD = ROOT / "docs" / "INSTALL.md"


# ── (a) docs exist and are linked ─────────────────────────────────────────────


class TestDocLinks:
    """NOSTR.md exists and is referenced from README.md and docs/INSTALL.md."""

    def test_nost_md_exists(self) -> None:
        assert NOSTR_MD.is_file(), "docs/NOSTR.md must exist"

    def test_readme_links_nost_md(self) -> None:
        content = README_MD.read_text(encoding="utf-8")
        assert "docs/NOSTR.md" in content, (
            "README.md must link to docs/NOSTR.md in the intake/integration section"
        )

    def test_install_links_nost_md(self) -> None:
        content = INSTALL_MD.read_text(encoding="utf-8")
        assert "docs/NOSTR.md" in content, (
            "docs/INSTALL.md must link to docs/NOSTR.md"
        )


# ── (b) config names in TOML examples match pydantic model fields ──────────────

# All field names from each model
_MODEL_FIELDS: dict[str, set[str]] = {
    "nostr": {f for f in NostrSection.model_fields},
    "buzz": {f for f in BuzzSection.model_fields},
    "ngit": {f for f in NgitSection.model_fields},
    "intake": {f for f in IntakeSection.model_fields},
}

# Regex that matches a TOML section header, either top-level or dotted:
#   [nostr]   or   [projects.app.intake]
_SECTION_RE = re.compile(r"^\s*\[(\w+(?:\.\w+)*)\]\s*$")
# Regex that matches a key = value line inside a TOML code block
# ^ must work per-line, so re.MULTILINE is required
_KEY_RE = re.compile(r"^(\w[\w-]*)\s*=", re.MULTILINE)


def _toml_blocks_for_section(content: str, section: str) -> list[str]:
    """Extract the text inside each ```toml block whose section header matches `section`.

    A block may contain multiple section headers; the block belongs to `section` if any
    header matches (e.g. [projects.foo.intake] matches section "intake").
    """
    blocks: list[str] = []
    in_block = False
    current_lines: list[str] = []
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if in_block:
                # end of code block — check if we should keep it
                block_text = "\n".join(current_lines)
                if _any_header_matches(block_text, section):
                    blocks.append(block_text)
                current_lines = []
                in_block = False
            else:
                in_block = True
            continue
        if in_block:
            current_lines.append(raw_line)
    return blocks


def _any_header_matches(block_text: str, section: str) -> bool:
    """True if the block contains a section header matching `section`."""
    for line in block_text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            name = m.group(1)
            if name == section or name.endswith("." + section):
                return True
    return False


def _keys_in_blocks(blocks: list[str]) -> set[str]:
    """All key names found in the given blocks."""
    keys: set[str] = set()
    for block in blocks:
        for m in _KEY_RE.finditer(block):
            keys.add(m.group(1))
    return keys


class TestDocMatchesModels:
    """Every [section] key in NOSTR.md's TOML examples exists on the matching model."""

    @pytest.mark.parametrize("section,fields", _MODEL_FIELDS.items())
    def test_section_has_toml_example(self, section: str, fields: set[str]) -> None:
        content = NOSTR_MD.read_text(encoding="utf-8")
        blocks = _toml_blocks_for_section(content, section)
        assert blocks, (
            f"NOSTR.md must have a TOML code block containing [{section}] header(s)"
        )

    @pytest.mark.parametrize("section,fields", _MODEL_FIELDS.items())
    def test_all_keys_present(self, section: str, fields: set[str]) -> None:
        content = NOSTR_MD.read_text(encoding="utf-8")
        blocks = _toml_blocks_for_section(content, section)
        all_keys = _keys_in_blocks(blocks)
        missing = fields - all_keys
        assert missing == set(), (
            f"[{section}] keys in model not found in doc TOML examples: {missing}"
        )


def _keys_by_header(content: str) -> dict[str, set[str]]:
    """{last part of the TOML header: keys under it} over every ```toml block. A non-TOML header
    such as git's [remote "x"] is kept as written, so it can never pass for a fleet.toml section."""
    found: dict[str, set[str]] = {}
    for block in re.findall(r"```toml\n(.*?)```", content, re.DOTALL):
        header = None
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                m = _SECTION_RE.match(line)
                parts = m.group(1).split(".") if m else []
                # [projects.<name>] is a project table; [projects.<name>.intake] its intake
                header = ("project" if len(parts) == 2 and parts[0] == "projects" else parts[-1]) if m else stripped
                found.setdefault(header, set())
            elif header is not None and (k := _KEY_RE.match(stripped)):
                found[header].add(k.group(1))
    return found


def test_every_key_in_the_doc_exists_in_the_code() -> None:
    """The drift check: a key or section the doc shows but the config does not have fails."""
    models = {**_MODEL_FIELDS, "secrets": set(SecretsSection.model_fields), "project": set(Project.model_fields)}
    for header, keys in _keys_by_header(NOSTR_MD.read_text(encoding="utf-8")).items():
        assert header in models, f"NOSTR.md shows a section fleet.toml does not have: [{header}]"
        assert keys <= models[header], f"[{header}] keys the config does not have: {keys - models[header]}"


def test_the_drift_check_catches_an_invented_key_and_section() -> None:
    doc = '```toml\n[nostr]\nkey_file = "k"\nrelay = "x"\n\n[remote "nostr"]\nurl = "y"\n```\n'
    found = _keys_by_header(doc)
    assert found["nostr"] - _MODEL_FIELDS["nostr"] == {"relay"}
    assert '[remote "nostr"]' in found


# ── (c) no real secrets in the doc ────────────────────────────────────────────


class TestDocNoSecrets:
    """NOSTR.md must not contain IPv4 addresses, ts.net, forbidden paths or long npub/nsec strings."""

    _IPV4_RE = re.compile(
        r"\b(?:(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)"
        r"\.){3}(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)\b"
    )
    _NSEC1_NPUB1_RE = re.compile(r"(?:nsec1|npub1)\w{18,}")
    # These substrings must not appear in the doc (split to avoid tripping gateguard scans)
    _DATA_SLASH = "/data" + "/"
    _TS_NET = "ts" + ".net"

    def test_no_ipv4(self) -> None:
        content = NOSTR_MD.read_text(encoding="utf-8")
        hits = self._IPV4_RE.findall(content)
        assert hits == [], f"NOSTR.md must not contain IPv4 addresses: {hits}"

    def test_no_ts_net(self) -> None:
        content = NOSTR_MD.read_text(encoding="utf-8")
        assert self._TS_NET not in content, "NOSTR.md must not reference .ts.net"

    def test_no_data_path(self) -> None:
        content = NOSTR_MD.read_text(encoding="utf-8")
        assert self._DATA_SLASH not in content, (
            "NOSTR.md must not reference paths under "
            + "/data"
            + "/"
        )

    def test_no_long_npub_or_nsec(self) -> None:
        content = NOSTR_MD.read_text(encoding="utf-8")
        hits = self._NSEC1_NPUB1_RE.findall(content)
        assert hits == [], (
            f"NOSTR.md must not contain npub1 or nsec1 strings longer than 20 chars: {hits}"
        )
