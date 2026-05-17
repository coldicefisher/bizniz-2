"""Per-language tokenizers for the CPD detector.

Each language has its own regex lexer and normalization rules. The
output shape is uniform — a list of normalized tokens + parallel
list of source line numbers — so the downstream CPD algorithm in
``cpd.py`` is language-agnostic.

Normalization collapses string and number literals to placeholders
(``STR``, ``NUM``) so the detector finds STRUCTURAL duplicates even
when surface-level constants differ.

Multi-line strings (Python triple-quoted, TS backtick template
literals) are handled in a pre-pass that replaces them with a
single ``STR`` placeholder before line-level tokenization runs.
This keeps the line-level regex simple and language-agnostic.

Ported from MUSE's cpd-analysis.py.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple


# Lines longer than this are skipped — they're almost always data
# dumps (huge string literals, generated bindings), not logic.
MAX_LINE_CHARS = 500


# ── Pre-pass: collapse multi-line strings to a placeholder ───────


# We use a non-quote sentinel that won't appear as a normal token —
# it gets re-normalized to STR by the per-language tokenizer.
_MULTILINE_STRING_SENTINEL = "__MULTILINE_STR__"


def _build_python_prepass() -> List[re.Pattern]:
    # Triple-quoted strings, with optional prefix (f/r/b/rb/...).
    # ``re.DOTALL`` so ``.`` matches newlines inside the string.
    prefix = r"(?:f|r|b|rb|br|fr|rf|F|R|B|RB|BR|FR|RF)?"
    return [
        re.compile(
            prefix + r"\"\"\".*?\"\"\"", re.DOTALL,
        ),
        re.compile(
            prefix + r"'''.*?'''", re.DOTALL,
        ),
    ]


def _build_typescript_prepass() -> List[re.Pattern]:
    # Backtick-delimited template literals can span multiple lines.
    return [
        re.compile(r"`(?:\\.|[^`\\])*`", re.DOTALL),
    ]


_PREPASS_BY_LANG: Dict[str, List[re.Pattern]] = {
    "python": _build_python_prepass(),
    "typescript": _build_typescript_prepass(),
}


def _strip_multiline_strings(text: str, lang: str) -> str:
    """Replace every multi-line string literal with the sentinel.
    Preserves newlines so line numbers stay correct downstream — the
    replacement is sentinel + same number of trailing ``\n`` as the
    original matched text contained."""
    patterns = _PREPASS_BY_LANG.get(lang, [])
    for pat in patterns:
        def _repl(m):
            matched = m.group(0)
            newlines = matched.count("\n")
            return _MULTILINE_STRING_SENTINEL + ("\n" * newlines)
        text = pat.sub(_repl, text)
    return text


# ── Line-level token regex per language ──────────────────────────


# Python — variables, single-line strings, numbers, identifiers,
# operators, punctuation. Multi-line strings have already been
# replaced with the sentinel by the pre-pass above.
_PYTHON_TOKEN_RE = re.compile(
    r"(?:f|r|b|rb|br|fr|rf|F|R|B|RB|BR|FR|RF)?\"(?:\\.|[^\"\\])*\""
    r"|(?:f|r|b|rb|br|fr|rf|F|R|B|RB|BR|FR|RF)?'(?:\\.|[^'\\])*'"
    r"|0[xX][0-9a-fA-F]+"
    r"|\d+\.\d+"
    r"|\d+"
    r"|[A-Za-z_][A-Za-z0-9_]*"
    r"|==|!=|<=|>=|->|=>|//|\*\*|<<|>>|\+=|-=|\*=|/=|%=|&=|\|=|\^="
    r"|[{}()\[\];,:=<>!+\-*/%&|^~.?@#]"
)


# TypeScript / JavaScript — single-line strings (backtick handled
# in pre-pass), numbers, identifiers (incl. $/_), operators.
_TS_TOKEN_RE = re.compile(
    r"\"(?:\\.|[^\"\\])*\""
    r"|'(?:\\.|[^'\\])*'"
    r"|0[xX][0-9a-fA-F]+"
    r"|\d+\.\d+"
    r"|\d+"
    r"|[A-Za-z_$][A-Za-z0-9_$]*"
    r"|===|!==|==|!=|<=|>=|=>|<<|>>|>>>|\+\+|--|&&|\|\||\?\?|\?\.|\.\.\.|\*\*"
    r"|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=|>>>="
    r"|[{}()\[\];,:=<>!+\-*/%&|^~.?@#]"
)


_TOKENIZER_BY_LANG: Dict[str, re.Pattern] = {
    "python": _PYTHON_TOKEN_RE,
    "typescript": _TS_TOKEN_RE,
}


# Comment strippers — applied line-by-line after the multi-line
# string pre-pass.
_COMMENT_PATTERNS: Dict[str, List[re.Pattern]] = {
    "python": [
        re.compile(r"(^|\s)#.*$"),
    ],
    "typescript": [
        re.compile(r"//.*$"),
    ],
}


_LANGUAGE_BY_EXT: Dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".mjs": "typescript",
    ".cjs": "typescript",
}


def detect_language(path: str) -> Optional[str]:
    """Return the language key for a file path based on extension,
    or ``None`` if unknown."""
    for ext, lang in _LANGUAGE_BY_EXT.items():
        if path.endswith(ext):
            return lang
    return None


# ── Normalization ────────────────────────────────────────────────


def _normalize_token(tok: str, lang: str) -> str:
    """Map a raw token to its normalized form.

    Rules (apply to both languages):
    - The multi-line-string sentinel → ``STR``
    - Quoted strings (single-line) → ``STR``
    - Numeric literals → ``NUM``
    - Identifiers stay as themselves (preserves meaningful signal)
    - Operators / punctuation stay verbatim
    """
    if tok == _MULTILINE_STRING_SENTINEL:
        return "STR"
    # Plain quoted strings.
    if tok and tok[0] in ('"', "'", "`"):
        return "STR"
    # Prefixed strings (Python: f"...", r"...", b"...", rb"...").
    if len(tok) >= 2:
        first_quote = -1
        for i, c in enumerate(tok):
            if c in ('"', "'", "`"):
                first_quote = i
                break
        if first_quote > 0:
            head = tok[:first_quote].lower()
            if all(c in "frb" for c in head):
                return "STR"
    if tok.startswith("0x") or tok.startswith("0X"):
        return "NUM"
    if tok and tok[0].isdigit():
        return "NUM"
    return tok


# ── Public API ───────────────────────────────────────────────────


def tokenize_text(
    text: str,
    language: str,
) -> Tuple[List[str], List[int], int, int]:
    """Tokenize source text.

    Returns ``(tokens, token_lines, total_lines, code_lines)``.

    - ``tokens`` — normalized token stream (parallel to ``token_lines``)
    - ``token_lines[i]`` — 1-based source line number the i-th token
      came from
    - ``total_lines`` — raw line count (including blanks/comments)
    - ``code_lines`` — non-blank, non-comment lines that survived
      the ``MAX_LINE_CHARS`` filter

    Consecutive runs of identical ``NUM`` or ``STR`` tokens collapse
    to a single instance. Consecutive commas also collapse.
    """
    token_re = _TOKENIZER_BY_LANG.get(language)
    if token_re is None:
        raise ValueError(f"unknown language: {language!r}")
    comment_patterns = _COMMENT_PATTERNS.get(language, [])

    # Pre-pass: collapse multi-line strings before line-level work
    # so they're treated as a single token rather than fragmenting
    # across the lexer.
    text = _strip_multiline_strings(text, language)

    tokens: List[str] = []
    token_lines: List[int] = []
    total_lines = 0
    code_lines = 0

    for ln, line in enumerate(text.splitlines(), 1):
        total_lines += 1
        stripped = line
        for pat in comment_patterns:
            stripped = pat.sub("", stripped)
        stripped = stripped.rstrip()
        if not stripped.strip():
            continue
        if len(stripped) > MAX_LINE_CHARS:
            continue
        code_lines += 1
        prev_norm = None
        for m in token_re.findall(stripped):
            norm = _normalize_token(m, language)
            if norm in ("NUM", "STR") and prev_norm == norm:
                continue
            if norm == "," and prev_norm == ",":
                continue
            tokens.append(norm)
            token_lines.append(ln)
            prev_norm = norm

    return tokens, token_lines, total_lines, code_lines


def tokenize_file(
    path: str,
    language: Optional[str] = None,
) -> Tuple[List[str], List[int], int, int]:
    """Tokenize one file. ``language`` defaults to detection from
    extension; raises ``ValueError`` if neither works."""
    if language is None:
        language = detect_language(path)
        if language is None:
            raise ValueError(
                f"cannot detect language for {path!r} — "
                f"pass ``language=`` explicitly"
            )
    with open(path, "r", errors="replace") as f:
        text = f.read()
    return tokenize_text(text, language)
