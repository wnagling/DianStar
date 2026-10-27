"""Bounded local JSON validation; remote schema references are forbidden."""
import json
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable


def valid_arguments(schema: dict, arguments: dict) -> bool:
    """Validate bounded JSON locally, never resolve a remote schema reference."""
    try:
        parameters = schema["function"]["parameters"]
        if (parameters.get("type") != "object" or parameters.get("additionalProperties") is not False
                or not isinstance(arguments, dict) or not safe_schema(parameters)):
            return False
        payload = json.dumps(arguments, allow_nan=False)
        if len(payload) > 32768:
            return False
        Draft202012Validator.check_schema(parameters)
        # GitHub's official update schemas allow empty text (e.g. clearing a PR
        # body). Its repository and query policy is checked by the adapter.
        official_github = schema["function"].get("name", "").startswith("mcp_github__")
        return Draft202012Validator(parameters, registry=Registry()).is_valid(arguments) and (official_github or nonempty_strings(arguments))
    except (KeyError, TypeError, ValueError, RecursionError, SchemaError, Unresolvable):
        return False


def safe_schema(value, depth: int = 0) -> bool:
    if depth > 32:
        return False
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#/")):
                return False
            if not safe_schema(item, depth + 1):
                return False
    elif isinstance(value, list):
        return all(safe_schema(item, depth + 1) for item in value)
    return True


def nonempty_strings(value, depth: int = 0) -> bool:
    if depth > 32:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return all(isinstance(key, str) and nonempty_strings(item, depth + 1) for key, item in value.items())
    if isinstance(value, list):
        return all(nonempty_strings(item, depth + 1) for item in value)
    return value is None or type(value) in {int, float, bool}
