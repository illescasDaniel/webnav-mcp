from __future__ import annotations

from webnav_mcp.outline_imports import drop_import_symbols, import_lines


def _at(name: str, line: int) -> dict:
	return {
		"name": name,
		"kind": 13,
		"range": {"start": {"line": line, "character": 0}, "end": {"line": line, "character": 1}},
	}


def test_given_single_and_multi_line_imports_when_scanned_then_every_line_of_each_statement_is_covered():
	# given
	source = 'import a from "a";\nimport {\n\tb,\n\tc,\n} from "b"\nimport "side-effect";\nconst x = 1;\n'
	# when / then
	assert sorted(import_lines(source)) == [0, 1, 2, 3, 4, 5]


def test_given_import_equals_attributes_and_type_import_when_scanned_then_each_is_one_statement():
	# given
	source = (
		'import fs = require("fs");\nimport Foo = Bar.Baz;\n'
		'import j from "./j.json" with { type: "json" };\nimport type { T } from "t";\nlet y;\n'
	)
	# when / then
	assert sorted(import_lines(source)) == [0, 1, 2, 3]


def test_given_a_line_that_only_mentions_import_when_scanned_then_it_is_not_an_import():
	# when / then
	assert import_lines('const importantly = 1;\n\tawait import("x");\nimportant();\n') == set()


def test_given_symbols_on_import_lines_when_dropped_then_only_own_declarations_remain():
	# given
	source = 'import { a,\n b } from "x";\nexport const c = 1;\n'
	# when
	kept = drop_import_symbols([_at("a", 0), _at("b", 1), _at("c", 2)], source)
	# then
	assert [s["name"] for s in kept] == ["c"]
