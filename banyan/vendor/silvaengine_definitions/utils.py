#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

from typing import Any, Dict

from silvaengine_utility import Serializer


def normalize_to_json(item: Any) -> Any:
    """Convert model objects or plain objects into JSON-serializable data."""
    if isinstance(item, dict):
        return Serializer.json_normalize(item)

    if hasattr(item, "attribute_values"):
        return Serializer.json_normalize(item.attribute_values)

    if hasattr(item, "__dict__"):
        return Serializer.json_normalize(
            {k: v for k, v in vars(item).items() if not k.startswith("_")}
        )
    return item


def normalize_model(model: Any) -> Dict[str, Any]:
    """
    Safely convert a PynamoDB model into a plain dict.

    Args:
        model: PynamoDB model instance

    Returns:
        Dictionary representation of the model
    """
    if hasattr(model, "__dict__") and "attribute_values" in model.__dict__:
        return normalize_to_json(model.__dict__["attribute_values"])
    return {}
