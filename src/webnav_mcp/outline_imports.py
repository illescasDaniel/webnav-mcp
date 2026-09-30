"""Keeps import bindings out of `outline`.

TypeScript's language server reports every imported name as a top-level `Variable`, which
drowns the file's own structure. The statements are located in the source text, so a name
imported over several lines is hidden too. `outline(detailed=true)` still lists them.
"""

from __future__ import annotations

import re
from typing import Any


# A statement's last line ends in the module specifier (optionally followed by an import attribute).
_ENDS_WITH_SPECIFIER = re.compile(r"""["'][^"']*["']\s*(?:(?:with|assert)\s*\{[^}]*\}\s*)?;?\s*(?://.*)?$""")
_IMPORT_EQUALS = re.compile(r"^import\s+(?:type\s+)?[\w$]+\s*=")
_MAX_IMPORT_LINES = 500


def import_lines(source: str) -> set[int]:
	"""0-based lines covered by top-level `import` statements."""
	lines = source.splitlines()
	covered: set[int] = set()
	i = 0
	while i < len(lines):
		if not re.match(r"import\b", lines[i]):
			i += 1
			continue
		end = i
		if not (_IMPORT_EQUALS.match(lines[i]) and not re.search(r"[\"']", lines[i])):
			while end < len(lines) and end - i < _MAX_IMPORT_LINES and not _ENDS_WITH_SPECIFIER.search(lines[end]):
				end += 1
			if end >= len(lines) or end - i >= _MAX_IMPORT_LINES:
				end = i  # no specifier found: treat it as a one-line statement
		covered.update(range(i, end + 1))
		i = end + 1
	return covered


def drop_import_symbols(symbols: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
	"""`symbols` without the ones declared on an import line (works for both `documentSymbol` shapes)."""
	covered = import_lines(source)
	if not covered:
		return symbols
	kept = []
	for sym in symbols:
		rng = sym.get("range") or (sym.get("location") or {}).get("range") or {}
		line = (rng.get("start") or {}).get("line")
		if line is None or line not in covered:
			kept.append(sym)
	return kept
