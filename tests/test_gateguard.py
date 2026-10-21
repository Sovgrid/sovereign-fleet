"""Tests for GateGuard verification engine."""

import tempfile
from pathlib import Path

from fleet.core.gateguard import GateGuard


def test_gateguard_ast_syntax_pass_and_fail():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Valid file
        valid_py = Path(tmpdir) / "valid.py"
        valid_py.write_text("def hello():\n    return 'world'\n")

        ok, errors = GateGuard.verify_python_syntax(tmpdir)
        assert ok is True
        assert len(errors) == 0

        # Syntax error file
        broken_py = Path(tmpdir) / "broken.py"
        broken_py.write_text("def broken_syntax(:\n")

        ok, errors = GateGuard.verify_python_syntax(tmpdir)
        assert ok is False
        assert len(errors) == 1
        assert "broken.py" in errors[0]


def test_gateguard_fact_forcing():
    claimed = ["src/auth.py", "tests/test_auth.py"]
    actual = ["src/auth.py"]  # Agent hallucinated changing test_auth.py

    ok, discrepancies = GateGuard.assert_fact_forcing(claimed, actual)
    assert ok is False
    assert len(discrepancies) == 1
    assert "tests/test_auth.py" in discrepancies[0]


def test_gateguard_crash_class_f821_undefined_name():
    with tempfile.TemporaryDirectory() as tmpdir:
        # File with an undefined name (syntactically valid AST, but runtime crash)
        bad_py = Path(tmpdir) / "undefined_name.py"
        bad_py.write_text("def broken_runtime():\n    return non_existent_symbol_12345\n")

        ok, errors = GateGuard.verify_python_syntax(tmpdir)
        assert ok is False
        assert any("F821" in err or "non_existent_symbol_12345" in err for err in errors)

