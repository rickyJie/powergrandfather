"""Unit tests for TaskDef parameter validation + substitution (F1)."""
import pytest
from csm.modules.automation.params import (
    ParamValidationError,
    coerce_and_check,
    substitute,
    validate_param_spec,
)


def test_validate_ok():
    validate_param_spec([
        {"name": "x", "type": "str"},
        {"name": "n", "type": "int", "default": 1},
    ])


def test_validate_rejects_dup_name():
    with pytest.raises(ParamValidationError, match="duplicate"):
        validate_param_spec([{"name": "x"}, {"name": "x"}])


def test_validate_rejects_invalid_name():
    with pytest.raises(ParamValidationError, match="name invalid"):
        validate_param_spec([{"name": "123bad"}])
    with pytest.raises(ParamValidationError, match="name invalid"):
        validate_param_spec([{"name": "with-dash"}])


def test_validate_rejects_bad_type():
    with pytest.raises(ParamValidationError, match="type="):
        validate_param_spec([{"name": "x", "type": "bytes"}])


def test_validate_rejects_non_list():
    with pytest.raises(ParamValidationError, match="must be a list"):
        validate_param_spec({"name": "x"})


def test_coerce_applies_defaults():
    spec = [
        {"name": "name", "type": "str", "default": "anon"},
        {"name": "n", "type": "int", "default": 3},
    ]
    out = coerce_and_check(spec, {})
    assert out == {"name": "anon", "n": 3}


def test_coerce_required_missing_raises():
    spec = [{"name": "id", "type": "str", "required": True}]
    with pytest.raises(ParamValidationError, match="missing required"):
        coerce_and_check(spec, {})


def test_coerce_type_int_from_string():
    spec = [{"name": "n", "type": "int"}]
    assert coerce_and_check(spec, {"n": "42"}) == {"n": 42}


def test_coerce_type_bool_variants():
    spec = [{"name": "b", "type": "bool"}]
    assert coerce_and_check(spec, {"b": "yes"})["b"] is True
    assert coerce_and_check(spec, {"b": "0"})["b"] is False
    assert coerce_and_check(spec, {"b": True})["b"] is True


def test_coerce_type_mismatch_raises():
    spec = [{"name": "n", "type": "int"}]
    with pytest.raises(ParamValidationError, match="parameter 'n'"):
        coerce_and_check(spec, {"n": "abc"})


def test_coerce_passes_through_extras():
    """Extra keys not declared in spec are passed through unchanged (backward compat)."""
    spec = [{"name": "x", "type": "str", "default": "a"}]
    out = coerce_and_check(spec, {"x": "b", "extra": "kept"})
    assert out == {"x": "b", "extra": "kept"}


def test_substitute_replaces_known():
    assert substitute("group={g} iter={n}", {"g": "alpha", "n": 3}) == "group=alpha iter=3"


def test_substitute_leaves_unknown_literal():
    assert substitute("hello {who}", {}) == "hello {who}"


def test_substitute_multiple_occurrences():
    assert substitute("{x}-{x}-{x}", {"x": "z"}) == "z-z-z"


def test_substitute_ignores_non_placeholder_braces():
    # Single brace, malformed name shouldn't crash
    assert substitute("{1bad} {-bad} ok", {}) == "{1bad} {-bad} ok"
