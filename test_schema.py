#!/usr/bin/env python3
"""Tests for tool-argument validation against each tool's advertised schema.

Pure and Krita-free: mcp_server.py is loaded by path and the bridge is patched
out, so the suite runs anywhere Python does.

    python3 test_schema.py
    python3 -m unittest test_schema -v

The contract under test: validate_arguments() returns one readable string per
violation of the JSON-Schema subset our tools use, with a path that locates the
offending value; call_tool() refuses a non-conforming request before the bridge
is contacted, as a BridgeError of kind `invalid_arguments` (which the server
turns into an `isError` tool result rather than a protocol error).
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mcp_server = _load("krita_schema_mcp_server", HERE / "mcp_server.py")
validate = mcp_server.validate_arguments


def schema_of(tool_name):
    return mcp_server.TOOLS_BY_NAME[tool_name]["inputSchema"]


class ValidRequestTest(unittest.TestCase):
    def assertValid(self, tool_name, arguments):
        self.assertEqual(validate(schema_of(tool_name), arguments), [])

    def test_tools_without_parameters(self):
        self.assertValid("status", {})
        self.assertValid("self_test", {})

    def test_create_document(self):
        self.assertValid("create_document", {
            "width": 400, "height": 300, "name": "x", "resolution_dpi": 96,
            "background": "#202830"})
        self.assertValid("create_document", {
            "width": 4, "height": 4, "background": [0, 0, 0, 255]})

    def test_document_may_be_an_index_or_a_name(self):
        self.assertValid("inspect_document", {"document": 0})
        self.assertValid("inspect_document", {"document": "My Doc"})

    def test_draw_with_every_command_kind(self):
        self.assertValid("draw", {"layer": "Art", "antialias": False,
                                  "commands": [
            {"type": "fill_rect", "x": 0, "y": 0, "w": 400.5, "h": 300,
             "color": "#ffffff"},
            {"type": "polygon", "points": [[40, 160], [110, 160.5]],
             "fill": [1, 2, 3, 4], "stroke_width": 2},
            {"type": "linear_gradient", "x": 0, "y": 0, "w": 9, "h": 9,
             "direction": "diagonal", "stops": [[0, "#000"], [1, "#fff"]]},
            {"type": "text", "x": 1, "y": 2, "text": "hi", "size": 20,
             "bold": True, "anchor": "center"},
        ]})

    def test_draw_command_fields_outside_the_schema_pass(self):
        # DRAW_COMMAND does not forbid extra fields, so the plugin's own
        # aliases (`width`/`height`, `stroke`) keep working.
        self.assertValid("draw", {"commands": [
            {"type": "rect", "x": 0, "y": 0, "width": 5, "height": 5,
             "stroke": "#000"}]})

    def test_transform_image_and_get_image(self):
        self.assertValid("transform_image", {"action": "crop", "x": 0, "y": 0,
                                             "width": 10, "height": 10})
        self.assertValid("transform_image", {"action": "rotate",
                                             "degrees": 12.5})
        self.assertValid("get_image", {"layer": "merged", "max_size": 300,
                                       "region": {"x": 1, "y": 2,
                                                  "width": 3, "height": 4}})

    def test_integral_float_is_an_integer(self):
        self.assertValid("get_pixel", {"x": 3.0, "y": 4})

    def test_no_arguments_object_at_all_is_fine_for_optional_tools(self):
        self.assertEqual(validate(schema_of("create_layer"), {}), [])


class TypeTest(unittest.TestCase):
    def test_wrong_scalar_type_names_path_and_value(self):
        errors = validate(schema_of("create_document"),
                          {"width": "400", "height": 300})
        self.assertEqual(len(errors), 1)
        self.assertIn("arguments.width", errors[0])
        self.assertIn("integer", errors[0])
        self.assertIn("string", errors[0])
        self.assertIn("400", errors[0])

    def test_bool_is_not_an_integer(self):
        errors = validate(schema_of("get_pixel"), {"x": True, "y": 1})
        self.assertEqual(len(errors), 1)
        self.assertIn("arguments.x", errors[0])
        self.assertIn("boolean", errors[0])

    def test_bool_is_not_a_number(self):
        errors = validate(schema_of("transform_image"),
                          {"action": "rotate", "degrees": False})
        self.assertEqual(len(errors), 1)
        self.assertIn("arguments.degrees", errors[0])

    def test_integer_is_a_number(self):
        self.assertEqual(validate({"type": "number"}, 3), [])

    def test_fractional_float_is_not_an_integer(self):
        errors = validate(schema_of("get_pixel"), {"x": 3.5, "y": 1})
        self.assertEqual(len(errors), 1)
        self.assertIn("arguments.x", errors[0])

    def test_integer_is_not_a_boolean_and_null_is_nothing(self):
        self.assertTrue(validate(schema_of("close_document"), {"save": 1}))
        self.assertTrue(validate(schema_of("close_document"),
                                 {"save": None}))

    def test_type_list_accepts_any_member_and_names_all_on_failure(self):
        schema = schema_of("create_document")
        self.assertEqual(validate(schema, {"width": 1, "height": 1,
                                           "background": "#fff"}), [])
        self.assertEqual(validate(schema, {"width": 1, "height": 1,
                                           "background": [1, 2, 3]}), [])
        errors = validate(schema, {"width": 1, "height": 1, "background": 7})
        self.assertEqual(len(errors), 1)
        self.assertIn("string or array", errors[0])

    def test_arguments_must_be_an_object(self):
        errors = validate(schema_of("status"), ["x"])
        self.assertEqual(len(errors), 1)
        self.assertIn("arguments", errors[0])
        self.assertIn("object", errors[0])

    def test_long_values_are_truncated_in_messages(self):
        errors = validate(schema_of("create_document"),
                          {"width": "9" * 5000, "height": 1})
        self.assertLess(len(errors[0]), 200)

    def test_custom_path_prefix(self):
        errors = validate({"type": "integer"}, "x", path="params.size")
        self.assertTrue(errors[0].startswith("params.size:"))


class RequiredAndAdditionalTest(unittest.TestCase):
    def test_every_missing_required_property_is_reported(self):
        errors = validate(schema_of("create_document"), {})
        self.assertEqual(len(errors), 2)
        joined = "\n".join(errors)
        self.assertIn("'width'", joined)
        self.assertIn("'height'", joined)
        self.assertIn("required", joined)

    def test_missing_required_in_nested_object(self):
        errors = validate(schema_of("get_image"),
                          {"region": {"x": 0, "y": 0, "width": 5}})
        self.assertEqual(len(errors), 1)
        self.assertIn("arguments.region", errors[0])
        self.assertIn("'height'", errors[0])

    def test_extra_property_lists_the_allowed_names(self):
        errors = validate(schema_of("create_layer"), {"bogus": 1})
        self.assertEqual(len(errors), 1)
        self.assertIn("arguments.bogus", errors[0])
        for allowed in ("document", "name", "type", "parent", "index",
                        "select", "opacity", "blending_mode", "visible",
                        "locked"):
            self.assertIn(allowed, errors[0])

    def test_tool_without_properties_rejects_extras(self):
        errors = validate(schema_of("status"), {"x": 1})
        self.assertEqual(len(errors), 1)
        self.assertIn("arguments.x", errors[0])
        self.assertIn("(none)", errors[0])

    def test_nested_object_without_the_flag_allows_extras(self):
        errors = validate(schema_of("get_image"),
                          {"region": {"width": 1, "height": 1, "extra": 1}})
        self.assertEqual(errors, [])

    def test_misspelt_property_and_missing_required_both_surface(self):
        errors = validate(schema_of("get_pixel"), {"x": 1, "yy": 2})
        self.assertEqual(len(errors), 2)
        joined = "\n".join(errors)
        self.assertIn("'y'", joined)
        self.assertIn("arguments.yy", joined)


class EnumTest(unittest.TestCase):
    def test_enum_violation_lists_the_choices(self):
        errors = validate(schema_of("create_layer"), {"type": "banana"})
        self.assertEqual(len(errors), 1)
        self.assertIn("arguments.type", errors[0])
        self.assertIn("banana", errors[0])
        self.assertIn("paintlayer", errors[0])
        self.assertIn("selectionmask", errors[0])

    def test_enum_member_passes(self):
        self.assertEqual(
            validate(schema_of("transform_image"), {"action": "flatten"}), [])

    def test_wrong_type_for_an_enum_is_one_error(self):
        errors = validate(schema_of("transform_image"), {"action": 3})
        self.assertEqual(len(errors), 1)
        self.assertIn("string", errors[0])

    def test_bool_does_not_match_a_numeric_enum_member(self):
        self.assertTrue(validate({"enum": [0, 1]}, True))
        self.assertEqual(validate({"enum": [0, 1]}, 1), [])

    def test_unknown_draw_type_names_the_valid_ones(self):
        errors = validate(schema_of("draw"),
                          {"commands": [{"type": "banana"}]})
        self.assertEqual(len(errors), 1)
        self.assertIn("arguments.commands[0].type", errors[0])
        self.assertIn("fill_rect", errors[0])


class NestedTest(unittest.TestCase):
    def draw(self, commands):
        return validate(schema_of("draw"), {"commands": commands})

    def test_error_path_reaches_the_offending_array_item(self):
        errors = self.draw([
            {"type": "fill_rect", "x": 0, "y": 0, "w": 1, "h": 1},
            {"type": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 1},
            {"type": "polyline", "points": [[0, 0], [1, 2, 3], [4, 5]]},
        ])
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith(
            "arguments.commands[2].points[1]:"), errors[0])
        self.assertIn("at most 2", errors[0])

    def test_error_path_reaches_a_scalar_inside_a_point(self):
        errors = self.draw([{"type": "polyline",
                             "points": [[0, 0], [1, "two"]]}])
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith(
            "arguments.commands[0].points[1][1]:"), errors[0])
        self.assertIn("number", errors[0])

    def test_short_point_is_a_min_items_error(self):
        errors = self.draw([{"type": "polygon", "points": [[1]]}])
        self.assertEqual(len(errors), 1)
        self.assertIn("arguments.commands[0].points[0]", errors[0])
        self.assertIn("at least 2", errors[0])

    def test_command_missing_its_type(self):
        errors = self.draw([{"x": 1}])
        self.assertEqual(len(errors), 1)
        self.assertIn("arguments.commands[0]", errors[0])
        self.assertIn("'type'", errors[0])

    def test_wrong_item_type(self):
        errors = self.draw(["fill_rect"])
        self.assertEqual(len(errors), 1)
        self.assertIn("arguments.commands[0]", errors[0])
        self.assertIn("object", errors[0])

    def test_empty_commands_violates_min_items(self):
        errors = self.draw([])
        self.assertEqual(len(errors), 1)
        self.assertIn("arguments.commands", errors[0])
        self.assertIn("at least 1", errors[0])

    def test_items_given_as_an_object_instead_of_a_list(self):
        errors = self.draw({"type": "rect"})
        self.assertEqual(len(errors), 1)
        self.assertIn("array", errors[0])

    def test_sibling_errors_are_all_reported(self):
        errors = self.draw([{"type": "text", "size": "big"},
                            {"type": "ellipse", "cx": True}])
        self.assertEqual(len(errors), 2)
        self.assertIn("commands[0].size", errors[0])
        self.assertIn("commands[1].cx", errors[1])


class KeywordTest(unittest.TestCase):
    """Each supported keyword on its own, with a schema built here."""

    def test_min_and_max_items(self):
        schema = {"type": "array", "minItems": 2, "maxItems": 3}
        self.assertTrue(validate(schema, [1]))
        self.assertEqual(validate(schema, [1, 2]), [])
        self.assertEqual(validate(schema, [1, 2, 3]), [])
        self.assertTrue(validate(schema, [1, 2, 3, 4]))

    def test_minimum_and_maximum(self):
        schema = {"type": "integer", "minimum": 0, "maximum": 255}
        self.assertEqual(validate(schema, 0), [])
        self.assertEqual(validate(schema, 255), [])
        low = validate(schema, -1)
        high = validate(schema, 256)
        self.assertEqual(len(low), 1)
        self.assertIn("minimum", low[0])
        self.assertEqual(len(high), 1)
        self.assertIn("maximum", high[0])

    def test_bounds_do_not_apply_to_other_types_or_booleans(self):
        self.assertEqual(validate({"minimum": 5}, "abc"), [])
        self.assertEqual(validate({"minimum": 5}, True), [])

    def test_unknown_keywords_are_ignored(self):
        schema = {"type": "string", "default": 5, "description": "d",
                  "format": "uri", "examples": [1]}
        self.assertEqual(validate(schema, "x"), [])

    def test_additional_properties_true_or_absent_allows_extras(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        self.assertEqual(validate(schema, {"a": "x", "b": 1}), [])

    def test_unsupported_type_name_is_a_schema_bug_not_a_pass(self):
        with self.assertRaises(ValueError):
            validate({"type": "null"}, 1)

    def test_tuples_count_as_arrays(self):
        self.assertEqual(validate({"type": "array"}, (1, 2)), [])


class ErrorCapTest(unittest.TestCase):
    def test_a_huge_bad_payload_yields_a_bounded_report(self):
        commands = [{"type": "nope"} for _ in range(50000)]
        errors = validate(schema_of("draw"), {"commands": commands})
        self.assertEqual(len(errors), mcp_server.MAX_SCHEMA_ERRORS)
        self.assertIn("further errors", errors[-1])
        self.assertEqual(sum("commands[" in e for e in errors),
                         mcp_server.MAX_SCHEMA_ERRORS - 1)
        self.assertLess(sum(len(e) for e in errors), 5000)

    def test_many_unknown_properties_are_capped_too(self):
        bogus = {"k{0}".format(i): i for i in range(500)}
        errors = validate(schema_of("status"), bogus)
        self.assertEqual(len(errors), mcp_server.MAX_SCHEMA_ERRORS)

    def test_exactly_at_the_cap_nothing_is_dropped(self):
        n = mcp_server.MAX_SCHEMA_ERRORS
        errors = validate({"type": "array", "items": {"type": "string"}},
                          list(range(n)))
        self.assertEqual(len(errors), n)
        self.assertFalse(any("further" in e for e in errors))


class SchemasUseOnlySupportedKeywordsTest(unittest.TestCase):
    """Guard: a keyword the validator ignores would silently stop protecting."""

    CONSTRAINTS = {"type", "enum", "properties", "required",
                   "additionalProperties", "items", "minItems", "maxItems",
                   "minimum", "maximum"}
    IGNORED = {"default", "description"}

    def walk(self, schema, where):
        extra = set(schema) - self.CONSTRAINTS - self.IGNORED
        self.assertEqual(extra, set(), "{0} uses unsupported keywords".format(
            where))
        for key, sub in schema.get("properties", {}).items():
            self.walk(sub, "{0}.{1}".format(where, key))
        if isinstance(schema.get("items"), dict):
            self.walk(schema["items"], where + "[]")

    def test_every_advertised_schema(self):
        for name, spec in mcp_server.TOOLS_BY_NAME.items():
            with self.subTest(tool=name):
                self.walk(spec["inputSchema"], name)

    def test_every_advertised_type_is_supported(self):
        def types(schema):
            declared = schema.get("type", [])
            yield from [declared] if isinstance(declared, str) else declared
            for sub in schema.get("properties", {}).values():
                yield from types(sub)
            if isinstance(schema.get("items"), dict):
                yield from types(schema["items"])

        allowed = {"string", "integer", "number", "boolean", "object",
                   "array"}
        for name, spec in mcp_server.TOOLS_BY_NAME.items():
            with self.subTest(tool=name):
                self.assertLessEqual(set(types(spec["inputSchema"])), allowed)


class CallToolTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(mcp_server.BRIDGE, "call",
                                    return_value={"ok": True})
        self.bridge = patcher.start()
        self.addCleanup(patcher.stop)

    def test_invalid_arguments_never_reach_the_bridge(self):
        with self.assertRaises(mcp_server.BridgeError) as caught:
            mcp_server.call_tool("create_layer", {"bogus": 1})
        self.assertEqual(caught.exception.kind, "invalid_arguments")
        message = str(caught.exception)
        self.assertIn("create_layer", message)
        self.assertIn("bogus", message)
        self.assertIn("allowed properties", message)
        self.assertIn("blending_mode", message)
        self.bridge.assert_not_called()

    def test_every_error_is_on_its_own_line(self):
        with self.assertRaises(mcp_server.BridgeError) as caught:
            mcp_server.call_tool("create_document", {})
        lines = [l for l in str(caught.exception).splitlines()
                 if l.startswith("- ")]
        self.assertEqual(len(lines), 2)
        self.bridge.assert_not_called()

    def test_wrong_type_is_refused(self):
        with self.assertRaises(mcp_server.BridgeError) as caught:
            mcp_server.call_tool("get_pixel", {"x": True, "y": 1})
        self.assertEqual(caught.exception.kind, "invalid_arguments")
        self.bridge.assert_not_called()

    def test_non_object_arguments_are_refused(self):
        with self.assertRaises(mcp_server.BridgeError) as caught:
            mcp_server.call_tool("status", ["x"])
        self.assertEqual(caught.exception.kind, "invalid_arguments")
        self.bridge.assert_not_called()

    def test_valid_call_reaches_the_bridge_unchanged(self):
        mcp_server.call_tool("create_layer", {"name": "A", "index": 2})
        self.bridge.assert_called_once_with("create_layer",
                                            {"name": "A", "index": 2})

    def test_missing_or_null_arguments_still_work(self):
        mcp_server.call_tool("status", None)
        mcp_server.call_tool("self_test", {})
        self.assertEqual([c.args for c in self.bridge.call_args_list],
                         [("status", {}), ("self_test", {})])

    def test_validation_sees_arguments_before_the_transform(self):
        # `action` is popped by the transform; validation must run first, so
        # a bad action is caught by the enum and an accepted one is mapped.
        with self.assertRaises(mcp_server.BridgeError) as caught:
            mcp_server.call_tool("transform_image", {"action": "teleport"})
        self.assertEqual(caught.exception.kind, "invalid_arguments")
        self.bridge.assert_not_called()
        mcp_server.call_tool("transform_image", {"action": "flatten"})
        self.bridge.assert_called_once_with("flatten_image", {})

    def test_run_python_without_code_never_runs(self):
        with self.assertRaises(mcp_server.BridgeError) as caught:
            mcp_server.call_tool("run_python", {"cod": "1"})
        self.assertEqual(caught.exception.kind, "invalid_arguments")
        self.assertIn("missing required property 'code'",
                      str(caught.exception))
        self.bridge.assert_not_called()

    def test_server_reports_it_as_a_tool_error_not_a_protocol_error(self):
        reply = mcp_server.Server().handle({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "create_layer", "arguments": {"bogus": 1}}})
        self.assertNotIn("error", reply)
        result = reply["result"]
        self.assertIs(result["isError"], True)
        text = result["content"][0]["text"]
        self.assertTrue(text.startswith("invalid_arguments: "), text)
        self.assertIn("bogus", text)
        self.bridge.assert_not_called()


class OptionalNullTest(unittest.TestCase):
    """`null` for an optional property means "omitted"; for a required one it
    is still an error."""

    def setUp(self):
        patcher = mock.patch.object(mcp_server.BRIDGE, "call",
                                    return_value={"ok": True})
        self.bridge = patcher.start()
        self.addCleanup(patcher.stop)

    def test_optional_null_is_accepted_and_not_forwarded(self):
        mcp_server.call_tool("create_layer", {
            "document": None, "name": "A", "parent": None, "index": None,
            "opacity": None, "visible": None})
        self.bridge.assert_called_once_with("create_layer", {"name": "A"})

    def test_required_null_is_rejected(self):
        with self.assertRaises(mcp_server.BridgeError) as caught:
            mcp_server.call_tool("create_document",
                                 {"width": None, "height": 3})
        self.assertEqual(caught.exception.kind, "invalid_arguments")
        self.assertIn("arguments.width", str(caught.exception))
        self.bridge.assert_not_called()

    def test_nulls_are_dropped_inside_nested_objects(self):
        mcp_server.call_tool("get_image", {
            "layer": None,
            "region": {"x": None, "y": 2, "width": 3, "height": 4}})
        self.bridge.assert_called_once_with(
            "get_image", {"region": {"y": 2, "width": 3, "height": 4}})

    def test_nulls_are_dropped_inside_array_items(self):
        mcp_server.call_tool("draw", {"layer": None, "commands": [
            {"type": "fill_rect", "x": 0, "y": 0, "w": 1, "h": 1,
             "color": "#fff", "fill": None, "stroke_width": None}]})
        self.bridge.assert_called_once_with("draw", {"commands": [
            {"type": "fill_rect", "x": 0, "y": 0, "w": 1, "h": 1,
             "color": "#fff"}]})

    def test_required_null_inside_a_nested_object_is_rejected(self):
        with self.assertRaises(mcp_server.BridgeError) as caught:
            mcp_server.call_tool("get_image", {
                "region": {"width": None, "height": 4}})
        self.assertIn("arguments.region.width", str(caught.exception))
        with self.assertRaises(mcp_server.BridgeError) as caught:
            mcp_server.call_tool("draw", {"commands": [{"type": None}]})
        self.assertIn("arguments.commands[0].type", str(caught.exception))
        self.bridge.assert_not_called()

    def test_null_under_an_undeclared_name_is_still_reported(self):
        with self.assertRaises(mcp_server.BridgeError) as caught:
            mcp_server.call_tool("create_layer", {"nmae": None})
        self.assertIn("arguments.nmae", str(caught.exception))
        self.bridge.assert_not_called()

    def test_a_wrong_type_is_not_mistaken_for_null(self):
        with self.assertRaises(mcp_server.BridgeError):
            mcp_server.call_tool("create_layer", {"name": 0})
        self.bridge.assert_not_called()

    def test_schemaless_objects_keep_their_nulls(self):
        mcp_server.call_tool("apply_filter", {
            "filter": "blur", "settings": {"halfWidth": None}})
        self.bridge.assert_called_once_with(
            "apply_filter", {"filter": "blur",
                             "settings": {"halfWidth": None}})

    def test_callers_dict_is_not_modified(self):
        arguments = {"name": "A", "index": None}
        mcp_server.call_tool("create_layer", arguments)
        self.assertEqual(arguments, {"name": "A", "index": None})

    def test_transformed_tool_drops_nulls_too(self):
        mcp_server.call_tool("list_capabilities",
                             {"kind": "blending_modes",
                              "include_parameters": None})
        self.bridge.assert_called_once_with("list_blending_modes", {})


if __name__ == "__main__":
    unittest.main()
