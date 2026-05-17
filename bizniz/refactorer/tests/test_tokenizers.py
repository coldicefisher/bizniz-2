"""Tests for the per-language tokenizers feeding the CPD detector."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bizniz.refactorer.tokenizers import (
    detect_language,
    tokenize_file,
    tokenize_text,
    _normalize_token,
)


class TestDetectLanguage:
    @pytest.mark.parametrize("path,lang", [
        ("/a/b/c.py", "python"),
        ("/a/b/c.ts", "typescript"),
        ("/a/b/c.tsx", "typescript"),
        ("/a/b/c.js", "typescript"),
        ("/a/b/c.jsx", "typescript"),
        ("/a/b/c.cjs", "typescript"),
        ("/a/b/c.mjs", "typescript"),
    ])
    def test_known_extensions(self, path, lang):
        assert detect_language(path) == lang

    def test_unknown_extension(self):
        assert detect_language("/a/b/c.rb") is None
        assert detect_language("/a/b/c.md") is None
        assert detect_language("/a/b/c") is None


class TestNormalizeToken:
    @pytest.mark.parametrize("tok,expected", [
        ('"hello"', "STR"),
        ("'hello'", "STR"),
        ("`hello`", "STR"),
        ('"a b c"', "STR"),
        ('f"hello {x}"', "STR"),
        ('r"raw"', "STR"),
        ("0", "NUM"),
        ("42", "NUM"),
        ("3.14", "NUM"),
        ("0x1f", "NUM"),
        ("0X1F", "NUM"),
        ("my_var", "my_var"),
        ("Company", "Company"),
        ("def", "def"),
        ("==", "=="),
        ("(", "("),
    ])
    def test_normalization(self, tok, expected):
        assert _normalize_token(tok, "python") == expected


class TestTokenizePython:
    def test_simple_function(self):
        src = textwrap.dedent('''
            def add(a, b):
                return a + b
        ''').strip()
        tokens, _, _, _ = tokenize_text(src, "python")
        # Expected stream contains identifiers + operators + structure.
        assert "def" in tokens
        assert "add" in tokens
        assert "return" in tokens
        assert "+" in tokens
        # Should NOT contain literal commas etc as text.

    def test_strings_normalized(self):
        src = '''
        x = "hello"
        y = 'world'
        z = f"{x} {y}"
        '''
        tokens, _, _, _ = tokenize_text(src, "python")
        # All three string literals collapse to STR.
        assert tokens.count("STR") >= 1
        # ``x = STR`` … no raw "hello" lurking.
        assert "hello" not in tokens
        assert "world" not in tokens

    def test_numbers_normalized(self):
        src = "x = 42\ny = 3.14\nz = 0xff"
        tokens, _, _, _ = tokenize_text(src, "python")
        # Each numeric literal becomes NUM (consecutive collapse means
        # at least one NUM, no raw "42" / "3.14" / "0xff").
        assert "NUM" in tokens
        assert "42" not in tokens
        assert "3.14" not in tokens
        assert "0xff" not in tokens

    def test_consecutive_string_run_collapses(self):
        # The collapse rule fires only on adjacent IDENTICAL tokens
        # (e.g. ``STR STR`` from concatenated literals). With commas
        # between, each STR is preserved.
        src = "x = ['a' 'b' 'c' 'd' 'e']"   # Python implicit string concat
        tokens, _, _, _ = tokenize_text(src, "python")
        # All 5 string literals — but adjacent so collapse fires → 1 STR.
        assert tokens.count("STR") == 1

    def test_comments_stripped(self):
        src = textwrap.dedent('''
            # this is a comment
            x = 1  # trailing
            y = 2
        ''').strip()
        tokens, _, _, _ = tokenize_text(src, "python")
        assert "this" not in tokens
        assert "comment" not in tokens
        assert "trailing" not in tokens
        assert "x" in tokens
        assert "y" in tokens

    def test_line_numbers_parallel_to_tokens(self):
        src = "x = 1\ny = 2\nz = 3"
        tokens, lines, _, _ = tokenize_text(src, "python")
        assert len(tokens) == len(lines)
        # First token is on line 1, last on line 3.
        assert lines[0] == 1
        assert lines[-1] == 3

    def test_long_lines_skipped(self):
        src = "x = " + ("a" * 600) + "\ny = 2"
        tokens, _, _, code_lines = tokenize_text(src, "python")
        # Only the second line counts.
        assert code_lines == 1
        assert "y" in tokens

    def test_blank_lines_skipped(self):
        # splitlines() yields 4 elements for "x = 1\n\n\ny = 2\n":
        # ["x = 1", "", "", "y = 2"]. The trailing \n doesn't add a
        # fifth empty element.
        src = "x = 1\n\n\ny = 2\n"
        tokens, _, total, code = tokenize_text(src, "python")
        assert total == 4
        # Blanks don't count toward code_lines.
        assert code == 2


class TestTokenizeTypeScript:
    def test_simple_function(self):
        src = '''
        function add(a: number, b: number): number {
            return a + b;
        }
        '''
        tokens, _, _, _ = tokenize_text(src, "typescript")
        assert "function" in tokens
        assert "add" in tokens
        assert "return" in tokens

    def test_template_literal_normalized(self):
        src = "const x = `hello ${name}`;"
        tokens, _, _, _ = tokenize_text(src, "typescript")
        assert "STR" in tokens
        assert "hello" not in tokens
        # ${name} is part of the template literal string.

    def test_arrow_function(self):
        src = "const fn = (a, b) => a + b;"
        tokens, _, _, _ = tokenize_text(src, "typescript")
        assert "=>" in tokens

    def test_jsx_ish_braces(self):
        # JSX uses { } interleaved with HTML-ish — our tokenizer
        # just runs the regex; we don't try to parse JSX.
        src = '''
        const Btn = ({ label }) => <button>{label}</button>;
        '''
        tokens, _, _, _ = tokenize_text(src, "typescript")
        # ``Btn``, ``label``, ``button`` all show up.
        assert "Btn" in tokens
        assert "label" in tokens
        assert "button" in tokens

    def test_comments_stripped(self):
        src = "// header\nconst x = 1; // inline\nconst y = 2;"
        tokens, _, _, _ = tokenize_text(src, "typescript")
        assert "header" not in tokens
        assert "inline" not in tokens
        assert "x" in tokens
        assert "y" in tokens


class TestTokenizeFile:
    def test_uses_extension_for_language(self, tmp_path):
        py = tmp_path / "x.py"
        py.write_text("def f(): return 1", "utf-8")
        tokens, _, _, _ = tokenize_file(str(py))
        assert "def" in tokens

    def test_explicit_language_overrides_extension(self, tmp_path):
        unknown = tmp_path / "x.unknown"
        unknown.write_text("def f(): return 1", "utf-8")
        tokens, _, _, _ = tokenize_file(str(unknown), language="python")
        assert "def" in tokens

    def test_unknown_extension_raises_without_explicit_lang(self, tmp_path):
        unknown = tmp_path / "x.unknown"
        unknown.write_text("x = 1", "utf-8")
        with pytest.raises(ValueError, match="cannot detect language"):
            tokenize_file(str(unknown))

    def test_unknown_language_raises(self):
        with pytest.raises(ValueError, match="unknown language"):
            tokenize_text("x = 1", "ruby")
