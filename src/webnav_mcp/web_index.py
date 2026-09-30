"""Workspace-wide CSS custom-property and class/id selector index for webnav.

The CSS/HTML language servers each see one document at a time, so `var(--x)`
usages and `#id`/`.class` selectors can't be cross-referenced across files.
This is a pure-Python scanner, not a language server: no `@import`
resolution, no CSS parser, regex/brace-stack grade. It rescans on every call
on every call, but reuses each root's parsed index while that root's files are
unchanged: the cache key is the `(path, mtime_ns, size)` of every relevant
file, re-stat'd on each call, so an edit, add, or delete always invalidates
it. Only the parse is skipped, not the directory walk, which keeps the
invalidation story trivially correct.

One or more named roots are indexed separately, since each may define its
own values/markup and mixing them into one answer would be misleading (see
`build_workspace_index`; a project configures its roots via
`WEBNAV_MCP_ROOTS`, e.g. separating production assets from design-wireframe
HTML). Positions are 1-indexed lines, matching the rest of the MCP tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from mcp_nav_shared.exclude import EXCLUDED_DIR_NAMES


DEFAULT_ROOT_LABEL = "web"

_VAR_NAME_RE = re.compile(r"--[a-zA-Z0-9_-]+")
_VAR_DECL_RE = re.compile(r"(--[a-zA-Z0-9_-]+)\s*:\s*([^;{}]+);")
_VAR_USE_RE = re.compile(r"var\(\s*(--[a-zA-Z0-9_-]+)\s*(,)?")
_SELECTOR_TOKEN_RE = re.compile(r"[.#][a-zA-Z_-][a-zA-Z0-9_-]*")

_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
# `(?<![\w-])`, not `\b`: `\b` also matches inside `data-id` / `data-style`.
_STYLE_ATTR_RE = re.compile(r'(?<![\w-])style\s*=\s*"([^"]*)"', re.IGNORECASE)
# A `src=` attribute means the tag has no inline body to scan (the capture
# group would just be empty in that case, so this isn't strictly required —
# kept for clarity and to skip a wasted regex pass over long external files
# that got inlined between the tags by mistake).
_SCRIPT_BLOCK_RE = re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
# Quote is a backreference (group 1) so `id="x"` and `id='x'` both match; the
# value is group 2. Used for HTML markup and — since JS string literals may
# themselves quote HTML (`innerHTML = '<span class="x">'`) — for JS source too.
_ID_ATTR_RE = re.compile(r"""(?<![\w-])id\s*=\s*(["'])([^"']+)\1""", re.IGNORECASE)
_CLASS_ATTR_RE = re.compile(r"""(?<![\w-])class\s*=\s*(["'])([^"']*)\1""", re.IGNORECASE)

# JS string literals below accept backticks too (template literals); a `${…}`
# inside one ends the static part and is treated like a `+` concatenation.
_JS_SETPROPERTY_RE = re.compile(r"\.setProperty\(\s*[\"'`](--[a-zA-Z0-9_-]+)[\"'`]")
_JS_GETPROPERTYVALUE_RE = re.compile(r"\.getPropertyValue\(\s*[\"'`](--[a-zA-Z0-9_-]+)[\"'`]")
_JS_GET_ELEMENT_BY_ID_RE = re.compile(r"getElementById\(\s*[\"'`]([^\"'`]*)[\"'`]\s*(\+)?")
_JS_CLASSLIST_RE = re.compile(r"classList\.(add|remove|toggle|contains)\(([^)]*)\)")
# `querySelector(?:All)?`, not `querySelectorAll?` — the latter only makes the
# final `l` optional, so plain `querySelector(...)` would never match.
# An optional TypeScript type argument (`querySelector<HTMLElement>(...)`, one
# nesting level: `closest<HTMLElement | null>(...)`) may sit before the `(`.
_TS_TYPE_ARGS = r"(?:<[^()<>]*(?:<[^()<>]*>[^()<>]*)*>)?"
_JS_QUERY_RE = re.compile(
	r"\b(?:querySelector(?:All)?|closest|matches)" + _TS_TYPE_ARGS + r"\(\s*[\"'`]([^\"'`]*)[\"'`]"
)
_JS_CLASSNAME_ASSIGN_RE = re.compile(r"className\s*\+?=\s*[\"'`]([^\"'`]*)[\"'`]\s*(\+)?")
_TEMPLATE_EXPR_RE = re.compile(r"\$\{[^}]*\}")
# A local variable conventionally named like a class list (e.g. `mediaClass`,
# `rowClasses`) being built up with `+=` (`mediaClass += ' slide-in-next-start'`)
# rather than assigned straight to `.className`. Narrower than matching any
# `identifier += 'literal'`, which would flag unrelated string-building code.
# The exact identifier `className` is excluded — `_JS_CLASSNAME_ASSIGN_RE`
# above already covers it — so the two regexes don't double-record a hit.
_JS_CLASS_VAR_CONCAT_RE = re.compile(r"\b(?!className\b)\w*[Cc]lass\w*\s*\+=\s*[\"'`]([^\"'`]*)[\"'`]\s*(\+)?")
# A string literal, optionally followed by `+` (string concatenation) — used to
# find dynamic prefixes inside `classList.add(...)` call arguments.
_STRING_LITERAL_RE = re.compile(r"[\"'`]([^\"'`]*)[\"'`]\s*(\+)?")
# A whole string literal that is itself a bare identifier-like name (`"btn-save"`),
# not followed by `+`. Projects wrap DOM lookups in their own helpers
# (`onClick("btn-save", …)`, `bindDisclosure("btn-x", "panel-x")`) that no fixed
# list of DOM APIs can know about; an exact-name literal is a strong enough
# signal to report as a (lower-confidence) reference — see `_string_literal_hits`.
_BARE_NAME_LITERAL_RE = re.compile(r"""(["'`])([A-Za-z_][A-Za-z0-9_-]*)\1(?!\s*\+)""")


def _line_at(text: str, index: int) -> int:
	"""1-indexed line number for a character offset."""
	return text.count("\n", 0, index) + 1


def _strip_css_comments(text: str) -> str:
	return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL)


def _strip_js_comments(text: str) -> str:
	"""Drop `//` and `/* */` comments (keeping newlines so line numbers hold),
	but not comment-looking text inside string literals (`"http://x"`).
	' and " strings end at a newline, so a stray quote in a regex literal can't
	desync the scan past its own line."""
	if "//" not in text and "/*" not in text:
		return text
	out: list[str] = []
	i, n = 0, len(text)
	quote = ""
	while i < n:
		ch = text[i]
		if quote:
			out.append(ch)
			if ch == "\\" and i + 1 < n:
				out.append(text[i + 1])
				i += 2
				continue
			if ch == quote or (ch == "\n" and quote != "`"):
				quote = ""
			i += 1
		elif ch in "\"'`":
			quote = ch
			out.append(ch)
			i += 1
		elif text.startswith("//", i):
			end = text.find("\n", i)
			i = n if end < 0 else end
		elif text.startswith("/*", i):
			end = text.find("*/", i + 2)
			end = n if end < 0 else end + 2
			out.append("\n" * text.count("\n", i, end))
			i = end
		else:
			out.append(ch)
			i += 1
	return "".join(out)


def _static_prefix(literal: str, concat_follows: bool) -> tuple[str, bool]:
	"""A template literal's text up to its first `${…}`, flagged dynamic —
	the same "only a static prefix is known" shape as `"view-" + x`."""
	if "${" in literal:
		return literal.split("${", 1)[0], True
	return literal, concat_follows


def _strip_html_comments(text: str) -> str:
	return re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL)


@dataclass(frozen=True)
class VarDeclaration:
	name: str
	value: str
	context: str  # e.g. "@media (prefers-color-scheme: dark) › :root"
	file: str  # relative to the root
	line: int


@dataclass(frozen=True)
class VarUsage:
	name: str
	file: str
	line: int
	has_fallback: bool = False


@dataclass(frozen=True)
class SelectorHit:
	token: str  # "#id" or ".class"
	kind: str  # "css" | "html" | "js"
	file: str
	line: int
	detail: str = ""  # e.g. "getElementById", "classList.add", "CSS rule"
	dynamic: bool = False  # a JS hit built from string-concatenation (only a static prefix is known)


@dataclass
class RootIndex:
	name: str
	root: Path
	# Directory the recorded `file` paths are relative to (the workspace root in
	# multi-root setups); defaults to `root` itself.
	base: Path | None = None
	var_declarations: dict[str, list[VarDeclaration]] = field(default_factory=dict)
	var_usages: dict[str, list[VarUsage]] = field(default_factory=dict)
	selector_hits: dict[str, list[SelectorHit]] = field(default_factory=dict)
	# bare name (no `#`/`.`) -> (file, line) of each JS/TS string literal equal to it
	string_literals: dict[str, list[tuple[str, int]]] = field(default_factory=dict)

	def add_declaration(self, decl: VarDeclaration) -> None:
		self.var_declarations.setdefault(decl.name, []).append(decl)

	def add_usage(self, usage: VarUsage) -> None:
		self.var_usages.setdefault(usage.name, []).append(usage)

	def add_selector_hit(self, hit: SelectorHit) -> None:
		self.selector_hits.setdefault(hit.token, []).append(hit)


@dataclass
class _Block:
	selector: str
	start: int  # index just after the opening '{'
	end: int  # index of the matching '}' (-1 until closed)
	parent: _Block | None


def _parse_blocks(text: str) -> list[_Block]:
	"""Brace-stack pass over (comment-stripped) CSS: one `_Block` per `{...}`,
	each knowing its own selector text and parent — enough to build a context
	breadcrumb for anything nested inside it. Doesn't account for `{`/`}`
	inside CSS string values (none occur in this codebase)."""
	blocks: list[_Block] = []
	open_stack: list[_Block] = []
	buf_start = 0
	for i, ch in enumerate(text):
		if ch == "{":
			selector = re.sub(r"\s+", " ", text[buf_start:i]).strip()
			block = _Block(selector=selector, start=i + 1, end=-1, parent=open_stack[-1] if open_stack else None)
			open_stack.append(block)
			blocks.append(block)
			buf_start = i + 1
		elif ch == "}":
			if open_stack:
				open_stack.pop().end = i
			buf_start = i + 1
		elif ch == ";":
			# A declaration (`color: #abc;`) ends here; without this reset, the
			# next nested rule's selector text would swallow it.
			buf_start = i + 1
	return blocks


def _enclosing_block(blocks: list[_Block], idx: int) -> _Block | None:
	best: _Block | None = None
	for b in blocks:
		if b.end == -1:
			continue
		if b.start <= idx < b.end and (best is None or (b.end - b.start) < (best.end - best.start)):
			best = b
	return best


def _breadcrumb(block: _Block | None) -> str:
	parts: list[str] = []
	while block is not None:
		if block.selector:
			parts.append(block.selector)
		block = block.parent
	return " › ".join(reversed(parts))


def _scan_css_text(text: str, file_rel: str, root_index: RootIndex, *, line_offset: int = 0) -> None:
	text = _strip_css_comments(text)
	blocks = _parse_blocks(text)

	for block in blocks:
		if block.end == -1 or block.selector.startswith("@") or not block.selector:
			continue
		line = _line_at(text, block.start - 1) + line_offset
		for part in block.selector.split(","):
			for tok in _SELECTOR_TOKEN_RE.findall(part):
				root_index.add_selector_hit(
					SelectorHit(token=tok, kind="css", file=file_rel, line=line, detail="CSS rule")
				)

	for match in _VAR_DECL_RE.finditer(text):
		name, value = match.group(1), match.group(2).strip()
		context = _breadcrumb(_enclosing_block(blocks, match.start()))
		line = _line_at(text, match.start()) + line_offset
		root_index.add_declaration(VarDeclaration(name=name, value=value, context=context, file=file_rel, line=line))

	for match in _VAR_USE_RE.finditer(text):
		name = match.group(1)
		line = _line_at(text, match.start()) + line_offset
		root_index.add_usage(VarUsage(name=name, file=file_rel, line=line, has_fallback=bool(match.group(2))))


def _scan_html_text(text: str, file_rel: str, root_index: RootIndex) -> None:
	text = _strip_html_comments(text)

	for style_match in _STYLE_BLOCK_RE.finditer(text):
		inner = style_match.group(1)
		offset = _line_at(text, style_match.start(1)) - 1
		_scan_css_text(inner, file_rel, root_index, line_offset=offset)

	for attr_match in _STYLE_ATTR_RE.finditer(text):
		inner = attr_match.group(1)
		line = _line_at(text, attr_match.start(1))
		_scan_css_text(inner, file_rel, root_index, line_offset=line - 1)

	for script_match in _SCRIPT_BLOCK_RE.finditer(text):
		inner = script_match.group(1)
		offset = _line_at(text, script_match.start(1)) - 1
		_scan_js_text(inner, file_rel, root_index, line_offset=offset)

	_scan_markup_attrs(
		text, file_rel, root_index, kind="html", id_detail="id attribute", class_detail="class attribute"
	)


def _scan_markup_attrs(
	text: str,
	file_rel: str,
	root_index: RootIndex,
	*,
	kind: str,
	id_detail: str,
	class_detail: str,
	line_offset: int = 0,
) -> None:
	"""`id="x"`/`class="a b"` attributes in `text`. Shared by HTML markup
	(`_scan_html_text`) and by JS source (`_scan_js_text`), since JS often
	builds markup from string literals (`innerHTML = '<span class="x">'`)."""
	for match in _ID_ATTR_RE.finditer(text):
		line = _line_at(text, match.start()) + line_offset
		# `id="row-${i}"` (JS template): only the static prefix is known.
		name, dynamic = _static_prefix(match.group(2), False)
		if name:
			root_index.add_selector_hit(
				SelectorHit(token=f"#{name}", kind=kind, file=file_rel, line=line, detail=id_detail, dynamic=dynamic)
			)

	for match in _CLASS_ATTR_RE.finditer(text):
		line = _line_at(text, match.start()) + line_offset
		for token in match.group(2).split():
			name, dynamic = _static_prefix(token, False)
			if not name:
				continue
			root_index.add_selector_hit(
				SelectorHit(token=f".{name}", kind=kind, file=file_rel, line=line, detail=class_detail, dynamic=dynamic)
			)


def _js_selector_tokens_from_string(value: str) -> list[tuple[str, bool]]:
	"""`(token, dynamic)` pairs; a token cut short by a template `${…}` is a
	dynamic prefix (`.item-${n}` -> `.item-`)."""
	value = _TEMPLATE_EXPR_RE.sub("\x00", value)
	return [(m.group(0), value[m.end() : m.end() + 1] == "\x00") for m in _SELECTOR_TOKEN_RE.finditer(value)]


def _literal_tokens(literal: str, concat_follows: bool) -> list[tuple[str, bool]]:
	"""Split a class-list string literal into whitespace-separated tokens,
	tagging the *last* one as a dynamic prefix when something is concatenated
	right after it (`"tool-status resolution-" + x`). A trailing space in the
	literal means the concatenation starts a fresh, separate class name rather
	than extending this one, so nothing is marked dynamic in that case."""
	tokens = literal.split()
	if not tokens:
		return []
	ends_with_space = literal[-1:].isspace()
	dynamic_prefix = concat_follows and not ends_with_space
	return [(tok, dynamic_prefix and i == len(tokens) - 1) for i, tok in enumerate(tokens)]


def _scan_js_text(text: str, file_rel: str, root_index: RootIndex, *, line_offset: int = 0) -> None:
	text = _strip_js_comments(text)

	for match in _JS_SETPROPERTY_RE.finditer(text):
		line = _line_at(text, match.start()) + line_offset
		root_index.add_usage(VarUsage(name=match.group(1), file=file_rel, line=line, has_fallback=True))

	for match in _JS_GETPROPERTYVALUE_RE.finditer(text):
		line = _line_at(text, match.start()) + line_offset
		root_index.add_usage(VarUsage(name=match.group(1), file=file_rel, line=line, has_fallback=True))

	for match in _JS_GET_ELEMENT_BY_ID_RE.finditer(text):
		literal, has_concat = _static_prefix(match.group(1), bool(match.group(2)))
		if not literal:
			continue
		line = _line_at(text, match.start()) + line_offset
		root_index.add_selector_hit(
			SelectorHit(
				token=f"#{literal}",
				kind="js",
				file=file_rel,
				line=line,
				detail="getElementById",
				dynamic=has_concat,
			)
		)

	for match in _JS_CLASSLIST_RE.finditer(text):
		method, args = match.group(1), match.group(2)
		line = _line_at(text, match.start()) + line_offset
		for lit_match in _STRING_LITERAL_RE.finditer(args):
			literal, concat_follows = _static_prefix(lit_match.group(1), bool(lit_match.group(2)))
			for token, dynamic in _literal_tokens(literal, concat_follows):
				root_index.add_selector_hit(
					SelectorHit(
						token=f".{token}",
						kind="js",
						file=file_rel,
						line=line,
						detail=f"classList.{method}",
						dynamic=dynamic,
					)
				)

	for match in _JS_QUERY_RE.finditer(text):
		line = _line_at(text, match.start()) + line_offset
		for token, dynamic in _js_selector_tokens_from_string(match.group(1)):
			root_index.add_selector_hit(
				SelectorHit(token=token, kind="js", file=file_rel, line=line, detail="querySelector", dynamic=dynamic)
			)

	for match in _JS_CLASSNAME_ASSIGN_RE.finditer(text):
		line = _line_at(text, match.start()) + line_offset
		literal, concat_follows = _static_prefix(match.group(1), bool(match.group(2)))
		for token, dynamic in _literal_tokens(literal, concat_follows):
			root_index.add_selector_hit(
				SelectorHit(token=f".{token}", kind="js", file=file_rel, line=line, detail="className", dynamic=dynamic)
			)

	for match in _JS_CLASS_VAR_CONCAT_RE.finditer(text):
		line = _line_at(text, match.start()) + line_offset
		literal, concat_follows = _static_prefix(match.group(1), bool(match.group(2)))
		for token, dynamic in _literal_tokens(literal, concat_follows):
			root_index.add_selector_hit(
				SelectorHit(
					token=f".{token}", kind="js", file=file_rel, line=line, detail="class-var +=", dynamic=dynamic
				)
			)

	for match in _BARE_NAME_LITERAL_RE.finditer(text):
		line = _line_at(text, match.start()) + line_offset
		root_index.string_literals.setdefault(match.group(2), []).append((file_rel, line))

	_scan_markup_attrs(
		text,
		file_rel,
		root_index,
		kind="js",
		id_detail="id attribute in JS string",
		class_detail="class attribute in JS string",
		line_offset=line_offset,
	)


_SCRIPT_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx")
_INDEXED_SUFFIXES = (".css", ".html", *_SCRIPT_SUFFIXES)

# (root, name, base) -> (signature, index); see the module docstring.
_ROOT_CACHE: dict[tuple[Path, str, Path], tuple[tuple[tuple[str, int, int], ...], RootIndex]] = {}


def _relevant_files(root: Path) -> list[Path]:
	if not root.is_dir():
		return []
	files = [
		p
		for p in root.rglob("*")
		if p.is_file()
		and p.suffix.lower() in _INDEXED_SUFFIXES
		and EXCLUDED_DIR_NAMES.isdisjoint(p.relative_to(root).parts)
	]
	return sorted(files)


def _signature(files: list[Path]) -> tuple[tuple[str, int, int], ...]:
	entries: list[tuple[str, int, int]] = []
	for path in files:
		try:
			stat = path.stat()
		except OSError:
			continue
		entries.append((str(path), stat.st_mtime_ns, stat.st_size))
	return tuple(entries)


def build_root_index(root: Path, name: str, workspace_root: Path | None = None) -> RootIndex:
	"""Scan `root` for CSS/HTML/JS/TS files. Recorded `file` paths are relative to
	`workspace_root` (defaulting to `root` itself) so multi-root setups
	(`WEBNAV_MCP_ROOTS`) report paths consistently with every other webnav
	tool — root-relative paths would otherwise look wrong/ambiguous whenever
	a named root isn't the workspace root."""
	base = workspace_root if workspace_root is not None else root
	files = _relevant_files(root)
	signature = _signature(files)
	cached = _ROOT_CACHE.get((root, name, base))
	if cached is not None and cached[0] == signature:
		return cached[1]
	root_index = RootIndex(name=name, root=root, base=base)
	for path in files:
		file_rel = str(path.relative_to(base)).replace("\\", "/")
		try:
			text = path.read_text(encoding="utf-8")
		except (OSError, UnicodeDecodeError):
			continue  # unreadable or non-UTF-8: skip the file rather than fail every index call
		suffix = path.suffix.lower()
		if suffix == ".css":
			_scan_css_text(text, file_rel, root_index)
		elif suffix == ".html":
			_scan_html_text(text, file_rel, root_index)
		elif suffix in _SCRIPT_SUFFIXES:
			_scan_js_text(text, file_rel, root_index)
	_ROOT_CACHE[(root, name, base)] = (signature, root_index)
	return root_index


def build_workspace_index(workspace_root: Path, roots: list[tuple[str, Path]] | None = None) -> list[RootIndex]:
	"""Build one `RootIndex` per configured root. `roots` is a list of
	`(label, absolute_path)` pairs; when omitted, indexes the whole
	`workspace_root` as a single root labeled `DEFAULT_ROOT_LABEL` (see
	`parse_roots_env` for how a project's `WEBNAV_MCP_ROOTS` env var becomes
	this list — server.py resolves it once at startup)."""
	if roots is None:
		roots = [(DEFAULT_ROOT_LABEL, workspace_root)]
	return [build_root_index(root, name, workspace_root) for name, root in roots]


def parse_roots_env(raw: str, workspace_root: Path) -> list[tuple[str, Path]]:
	"""Parse `WEBNAV_MCP_ROOTS` (`label=relative/path,label2=relative/path2`)
	into `(label, absolute_path)` pairs for `build_workspace_index`. A
	project with no such split (most projects) can leave this env var unset
	and get the single-root default instead."""
	roots: list[tuple[str, Path]] = []
	for part in raw.split(","):
		part = part.strip()
		if not part:
			continue
		label, sep, rel = part.partition("=")
		if not sep:
			raise ValueError(f"invalid WEBNAV_MCP_ROOTS entry {part!r}; expected label=relative/path")
		roots.append((label.strip(), (workspace_root / rel.strip()).resolve()))
	return roots


def _normalize_var_name(name: str) -> str:
	name = name.strip()
	return name if name.startswith("--") else f"--{name}"


def token_kind(token: str) -> str | None:
	"""`'id'`/`'class'` for a `#foo`/`.foo` token, else `None`."""
	if token.startswith("#"):
		return "id"
	if token.startswith("."):
		return "class"
	return None


# -- formatting --------------------------------------------------------------


def _group_usages_by_file(usages: list[VarUsage]) -> list[tuple[str, list[int]]]:
	groups: dict[str, list[int]] = {}
	for u in usages:
		groups.setdefault(u.file, []).append(u.line)
	return sorted((f, sorted(set(lines))) for f, lines in groups.items())


def _is_generated(file_rel: str, generated: tuple[str, ...]) -> bool:
	return any(file_rel == g or file_rel.startswith(g.rstrip("/") + "/") for g in generated)


def _file_label(file_rel: str, generated: tuple[str, ...]) -> str:
	"""Tag generated output (`WEBNAV_MCP_EXCLUDE`, e.g. `tsc`-emitted JS) so an
	agent edits the source instead — those hits stay in the index because a
	root's runtime usages may only exist there, but they are never the file to change."""
	return f"{file_rel} [generated]" if _is_generated(file_rel, generated) else file_rel


def _group_hits_by_file(hits: list[SelectorHit]) -> list[tuple[str, list[SelectorHit]]]:
	groups: dict[str, list[SelectorHit]] = {}
	for h in hits:
		groups.setdefault(h.file, []).append(h)
	return sorted(groups.items())


def format_css_var(
	indexes: list[RootIndex],
	name: str,
	*,
	generated: tuple[str, ...] = (),
	definitions_only: bool = False,
) -> str:
	var_name = _normalize_var_name(name)
	sections: list[str] = []
	found = False
	for idx in indexes:
		decls = idx.var_declarations.get(var_name, [])
		uses = [] if definitions_only else idx.var_usages.get(var_name, [])
		if not decls and not uses:
			continue
		found = True
		lines = [f"== {idx.name} =="]
		if decls:
			lines.append("Definitions:")
			lines += [f"  {d.file}:{d.line}  ({d.context or '(top level)'})  = {d.value}" for d in decls]
		else:
			lines.append("Definitions: (none)")
		if uses:
			by_file = _group_usages_by_file(uses)
			lines.append(f"Usages ({len(uses)} in {len(by_file)} file(s)):")
			lines += [f"  {_file_label(f, generated)}: " + ", ".join(f"L{n}" for n in ns) for f, ns in by_file]
		elif not definitions_only:
			lines.append("Usages: (none)")
		sections.append("\n".join(lines))
	if not found:
		return f"{var_name} is not defined or used anywhere under {_root_names(indexes)}."
	return f"{var_name}\n\n" + "\n\n".join(sections)


def _root_names(indexes: list[RootIndex]) -> str:
	names = [idx.name for idx in indexes]
	return " or ".join(names) if names else "any configured root"


def _dynamic_prefix_hits(idx: RootIndex, token: str) -> list[SelectorHit]:
	"""Dynamic hits stored under a *different*, shorter key that `token` could
	resolve to at runtime — e.g. a `getElementById("view-" + x)` hit stored
	under `#view-` is a plausible match for a query of `#view-components`."""
	hits: list[SelectorHit] = []
	for key, key_hits in idx.selector_hits.items():
		if key == token or not token.startswith(key):
			continue
		hits.extend(h for h in key_hits if h.dynamic)
	return hits


def _string_literal_hits(idx: RootIndex, token: str) -> list[SelectorHit]:
	"""JS/TS string literals exactly equal to `token`'s bare name, on lines no
	recognized DOM-API hit already covers (for any token: `getElementById("x")`
	must not also surface as a `.x` class reference) — typically an id/class passed to a
	project's own helper (`onClick("btn-save", …)`)."""
	covered = {(h.file, h.line) for hits in idx.selector_hits.values() for h in hits if h.kind == "js"}
	return [
		SelectorHit(token=token, kind="js", file=f, line=line, detail="string literal")
		for f, line in idx.string_literals.get(token[1:], [])
		if (f, line) not in covered
	]


def format_selector(
	indexes: list[RootIndex],
	token: str,
	*,
	generated: tuple[str, ...] = (),
	definitions_only: bool = False,
) -> str:
	kind = token_kind(token)
	if kind is None:
		return f"{token!r} must start with '#' (id) or '.' (class)."
	sections: list[str] = []
	found = False
	for idx in indexes:
		exact_hits = idx.selector_hits.get(token, [])
		prefix_hits = _dynamic_prefix_hits(idx, token)
		all_hits = list(exact_hits) + prefix_hits
		all_hits += _string_literal_hits(idx, token)
		if definitions_only:
			# A class is defined by its CSS rule(s), an id by its markup attribute.
			wanted = "css" if token.startswith(".") else "html"
			all_hits = [h for h in exact_hits if h.kind == wanted] or [h for h in exact_hits if h.kind == "css"]
		if not all_hits:
			continue
		found = True
		lines = [f"== {idx.name} =="]
		for kind_label in ("html", "css", "js"):
			# dict.fromkeys dedupes identical hits, e.g. `.a.x, .a .y {` records
			# `.a` twice for one rule line.
			kind_hits = list(dict.fromkeys(h for h in all_hits if h.kind == kind_label))
			if not kind_hits:
				continue
			by_file = _group_hits_by_file(kind_hits)
			lines.append(f"{kind_label.upper()} ({len(kind_hits)}):")
			for f, file_hits in by_file:
				parts = []
				for h in sorted(file_hits, key=lambda h: h.line):
					tag = f"L{h.line}"
					if h.dynamic and h.token != token:
						detail = (
							f"{h.detail}, dynamic partial match via {h.token!r}"
							if h.detail
							else f"dynamic partial match via {h.token!r}"
						)
					elif h.dynamic:
						detail = f"{h.detail}, dynamic partial match" if h.detail else "dynamic partial match"
					else:
						detail = h.detail
					if detail:
						tag += f" ({detail})"
					parts.append(tag)
				lines.append(f"  {_file_label(f, generated)}: " + ", ".join(parts))
		sections.append("\n".join(lines))
	if not found:
		return f"{token} was not found under {_root_names(indexes)}."
	return f"{token}\n\n" + "\n\n".join(sections)


# -- reference/definition/diagnostics enrichment ------------------------------

_CSS_TOKEN_UNDER_CURSOR_RE = re.compile(r"(--[a-zA-Z0-9_-]+|[.#][a-zA-Z_-][a-zA-Z0-9_-]*)")


def _attr_token_at(line_text: str, idx: int) -> str | None:
	"""`#name`/`.name` for a cursor on a word inside an HTML `id="..."`/`class="..."`
	value, where the markup spells the selector without its `#`/`.`."""
	for regex, prefix in ((_ID_ATTR_RE, "#"), (_CLASS_ATTR_RE, ".")):
		for attr in regex.finditer(line_text):
			if not attr.start(2) <= idx < attr.end(2):
				continue
			for word in re.finditer(r"\S+", attr.group(2)):
				if attr.start(2) + word.start() <= idx < attr.start(2) + word.end():
					return prefix + word.group()
			return None
	return None


def token_at_position(line_text: str, column: int) -> str | None:
	"""The `--var`, `#id` or `.class` token containing a 1-indexed `column`, if any,
	including a bare name inside an HTML `id`/`class` attribute value."""
	idx = column - 1
	attr_token = _attr_token_at(line_text, idx)
	if attr_token is not None:
		return attr_token
	for match in _CSS_TOKEN_UNDER_CURSOR_RE.finditer(line_text):
		if match.start() <= idx < match.end():
			return match.group(1)
	return None


def root_index_for_file(indexes: list[RootIndex], file_path: Path) -> tuple[RootIndex, str] | None:
	"""The index whose root contains `file_path`, plus the path exactly as that
	index recorded it (relative to `idx.base`, i.e. the workspace root in
	multi-root setups — not to the root folder itself)."""
	resolved = file_path.resolve()
	for idx in indexes:
		try:
			resolved.relative_to(idx.root.resolve())
			rel = str(resolved.relative_to((idx.base or idx.root).resolve())).replace("\\", "/")
		except ValueError:
			continue
		return idx, rel
	return None


def knows_selector(indexes: list[RootIndex], token: str) -> bool:
	"""True when any root's index has hits for `token` (`#id`/`.class`) or, for a bare-name
	string literal, its name — i.e. the index can answer a lookup for it."""
	return any(token in idx.selector_hits or token[1:] in idx.string_literals for idx in indexes)


def undefined_var_usages(idx: RootIndex) -> list[VarUsage]:
	return [
		usage
		for name, usages in idx.var_usages.items()
		for usage in usages
		if name not in idx.var_declarations and not usage.has_fallback
	]


def unreferenced_selectors(idx: RootIndex) -> list[str]:
	"""CSS-defined tokens with no HTML/JS reference. A dynamically-built JS hit
	elsewhere in the root (e.g. `getElementById("view-" + x)`, stored under
	`#view-`) counts as a reference for any token it's a prefix of, since the
	runtime value could plausibly be this one — see `_dynamic_prefix_hits`.
	So does a JS/TS string literal equal to the bare name (see
	`_BARE_NAME_LITERAL_RE`), e.g. an id passed to a project's own helper."""
	unreferenced = []
	for token, hits in idx.selector_hits.items():
		has_definition = any(h.kind == "css" for h in hits)
		has_reference = (
			any(h.kind in ("html", "js") and not h.dynamic for h in hits) or token[1:] in idx.string_literals
		)
		has_dynamic_prefix_reference = bool(_dynamic_prefix_hits(idx, token))
		if has_definition and not has_reference and not has_dynamic_prefix_reference:
			unreferenced.append(token)
	return sorted(unreferenced)


def diagnostics_for_file(idx: RootIndex, file_rel: str) -> list[str]:
	"""Index-derived warning lines for one file: undefined `var(--x)` usages
	(no fallback) and CSS selectors with no HTML/JS reference in this root.
	Dynamically-built JS selectors are skipped to avoid false positives."""
	warnings: list[str] = []
	for usage in undefined_var_usages(idx):
		if usage.file == file_rel:
			warnings.append(f"{usage.line}:1 [warning] var({usage.name}) is never defined in {idx.name}")
	unreferenced = set(unreferenced_selectors(idx))
	for token in unreferenced:
		for hit in idx.selector_hits.get(token, []):
			if hit.kind == "css" and hit.file == file_rel:
				warnings.append(f"{hit.line}:1 [warning] {token} is never referenced in {idx.name}'s HTML/JS")
	return sorted(warnings, key=lambda w: int(w.split(":", 1)[0]))
