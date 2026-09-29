#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import functools
import inspect
import logging
import math
import time
import traceback
import uuid
from typing import Any, Optional

from deepdiff import DeepDiff
from silvaengine_utility import Context, Serializer, Utility
from tenacity import retry, stop_after_attempt, wait_exponential

extract_data_for_data_diff = (
    lambda x, data_attributes_except_for_data_diff: Serializer.json_loads(
        Serializer.json_dumps(
            {
                k: v
                for k, v in x.__dict__["attribute_values"].items()
                if k not in data_attributes_except_for_data_diff
            }
        )
    )
)

## Get data_type
get_data_type = lambda x, y, z: x.__name__.replace(y, "").replace(z, "")


def monitor_decorator(original_function):
    @functools.wraps(original_function)
    def wrapper_function(*args, **kwargs):
        # Get the signature of the original function
        signature = inspect.signature(original_function)
        # Get the parameter names from the signature
        parameter_names = list(signature.parameters.keys())

        if "info" in parameter_names:
            logger = args[0].context.get("logger")
        elif "logger" in parameter_names:
            logger = args[0]

        start = time.perf_counter()
        result = original_function(*args, **kwargs)
        logger.info(
            f"Execute function: {original_function.__name__} spent {time.perf_counter() - start}s!"
        )
        return result

    return wrapper_function


def insert_update_decorator(
    keys={},
    model_funct=None,
    count_funct=None,
    type_funct=None,
    range_key_required=False,
    range_key_funct=None,
    data_attributes_except_for_data_diff=[],
    activity_history_funct=None,
):
    def actual_decorator(original_function):
        @functools.wraps(original_function)
        def wrapper_function(*args, **kwargs):
            info = kwargs.get("info")

            if not info and len(args) > 0:
                info = args[0]

            try:
                data_type = get_data_type(
                    original_function, "insert_update_", "_handler"
                )

                hash_key = kwargs.get(keys["hash_key"]) or info.context.get(
                    keys["hash_key"]
                )
                range_key = kwargs.get(keys["range_key"]) or (
                    range_key_funct(info, **kwargs)
                    if range_key_funct
                    else f"{uuid.uuid1().int % (10**20):020d}"
                )
                external_id = (
                    kwargs.get(keys["external_id"]) if keys.get("external_id") else None
                )

                count = (
                    count_funct(hash_key, range_key, external_id=external_id)
                    if external_id
                    else count_funct(hash_key, range_key)
                )

                entity = None
                if count > 0:
                    entity = (
                        model_funct(hash_key, range_key, external_id=external_id)
                        if external_id
                        else model_funct(hash_key, range_key)
                    )
                    old_data = extract_data_for_data_diff(
                        entity, data_attributes_except_for_data_diff
                    )
                    range_key = entity.__dict__["attribute_values"][keys["range_key"]]

                ## If count == 0 and range_key_required is False and range_key is not None, raise exception "the record is not found".
                if count == 0 and (
                    not range_key_required and kwargs.get(keys["range_key"]) is not None
                ):
                    raise Exception(
                        f"Cannot find the {data_type} with the {keys['hash_key']}/{keys['range_key']} ({hash_key}/{range_key}) at {time.strftime('%X')}."
                    )

                action = "inserted" if count == 0 else "updated"
                log = f"The {data_type} with the {keys['hash_key']}/{keys['range_key']} ({hash_key}/{range_key}) is {action} at {time.strftime('%X')}."

                if entity is None:
                    kwargs.update(
                        {keys["hash_key"]: hash_key, keys["range_key"]: range_key}
                    )
                kwargs.update({"entity": entity})

                ## Original functoin.
                original_function(*args, **kwargs)

                info.context.get("logger").info(log)

                entity = model_funct(hash_key, range_key)
                if action == "inserted":
                    if activity_history_funct:
                        activity_history_funct(
                            info,
                            **{
                                "id": f"{data_type}-{hash_key}-{range_key}",
                                "log": log,
                                "type": data_type,
                                "updated_by": kwargs.get("updated_by"),
                            },
                        )

                    return type_funct(info, entity)

                if activity_history_funct:
                    new_data = extract_data_for_data_diff(
                        entity, data_attributes_except_for_data_diff
                    )
                    data_diff = Serializer.json_loads(
                        Serializer.json_dumps(
                            DeepDiff(
                                old_data,
                                new_data,
                                ignore_order=True,
                            )
                        ),
                        parser_number=False,
                    )
                    if data_diff != {}:
                        activity_history_funct(
                            info,
                            **{
                                "id": f"{data_type}-{hash_key}-{range_key}",
                                "log": log,
                                "type": data_type,
                                "data_diff": data_diff,
                                "updated_by": kwargs.get("updated_by"),
                            },
                        )

                return type_funct(info, entity)
            except Exception as e:
                log = traceback.format_exc()
                info.context.get("logger").error(log)
                raise e

        return wrapper_function

    return actual_decorator


def resolve_list_decorator(
    attributes_to_get=[],
    list_type_class=None,
    type_funct=None,
    scan_index_forward=None,
):
    def actual_decorator(original_function):
        @functools.wraps(original_function)
        def wrapper_function(*args, **kwargs):
            try:
                data_type = get_data_type(
                    original_function, "resolve_", "_list_handler"
                )
                if "_list" in data_type:
                    data_type = data_type.replace("_list", "")

                page_number = kwargs.get("page_number", 1)
                limit = kwargs.get("limit", 100)

                ## Original functoin.
                inquiry_funct, count_funct, inquiry_args = original_function(
                    *args, **kwargs
                )

                ## Get total by scan.
                try:
                    if len(inquiry_args) == 0:
                        total = get_total_by_scan(
                            inquiry_funct, inquiry_args, attributes_to_get
                        )
                    else:
                        total = count_funct(*inquiry_args)
                        # If no hash key but filters, it will raise exception.
                except:
                    total = get_total_by_scan(
                        inquiry_funct, inquiry_args, attributes_to_get
                    )

                entities = results_pagination(
                    args[0],
                    inquiry_funct,
                    total,
                    limit,
                    page_number,
                    inquiry_args,
                    attributes_to_get,
                    scan_index_forward,
                )

                return list_type_class(
                    **{
                        f"{data_type}_list": [
                            type_funct(args[0], entity) for entity in entities
                        ],
                        "page_size": limit,
                        "page_number": page_number,
                        "total": total,
                    }
                )
            except Exception as e:
                log = traceback.format_exc()
                args[0].context.get("logger").error(log)
                raise e

        return wrapper_function

    return actual_decorator


def delete_decorator(
    keys={},
    model_funct=None,
):
    def actual_decorator(original_function):
        @functools.wraps(original_function)
        def wrapper_function(*args, **kwargs):
            try:
                data_type = get_data_type(original_function, "delete_", "_handler")

                hash_key = kwargs.get(keys["hash_key"]) or args[0].context.get(
                    keys["hash_key"]
                )
                range_key = kwargs[keys["range_key"]]

                entity = model_funct(hash_key, range_key)
                kwargs.update({"entity": entity})

                ## Original functoin.
                result = original_function(*args, **kwargs)

                args[0].context.get("logger").info(
                    f"The {data_type} with the {keys['hash_key']}/{keys['range_key']} ({hash_key}/{range_key}) is deleted at {time.strftime('%X')}."
                )
                return result
            except Exception as e:
                log = traceback.format_exc()
                args[0].context.get("logger").error(log)
                raise e

        return wrapper_function

    return actual_decorator


@retry(
    reraise=True,
    wait=wait_exponential(multiplier=1, max=60),
    stop=stop_after_attempt(5),
)
def results_pagination(
    info,
    query_scan,
    total,
    limit,
    page_number,
    args,
    attributes_to_get,
    scan_index_forward,
):
    ## Locate the last_evaluated_key for the specific page.
    last_evaluated_key = None
    info.context.get("logger").info(f"Locate page started at {time.strftime('%X')}.")
    if page_number > 1 and page_number <= math.ceil(total / limit):
        kwargs = {"attributes_to_get": attributes_to_get}
        if scan_index_forward is not None:
            kwargs.update({"scan_index_forward": scan_index_forward})
        results = query_scan(
            *args,
            **kwargs,
        )

        for i, entity in enumerate(results):
            if i + 1 == (page_number - 1) * limit:
                last_evaluated_key = results.last_evaluated_key
                break
    info.context.get("logger").info(f"Locate page finished at {time.strftime('%X')}.")

    ## Load the specific page by last_evaluated_key.
    if page_number <= math.ceil(total / limit):
        kwargs = {"last_evaluated_key": last_evaluated_key}
        if scan_index_forward is not None:
            kwargs.update({"scan_index_forward": scan_index_forward})
        results = query_scan(
            *args,
            **kwargs,
        )
    else:
        return []

    entities = []
    for i, entity in enumerate(results):
        entities.append(entity)
        if i + 1 == limit:
            break

    info.context.get("logger").info(f"Load page at {time.strftime('%X')}.")
    return entities


def get_total_by_scan(scan, args, attributes_to_get):
    total_results = scan(
        *args,
        attributes_to_get=attributes_to_get,
    )
    [entity for entity in total_results]
    total = total_results.total_count
    return total


def complete_table_name_decorator(cls: type) -> type:
    """
    Decorator to complete table name based on deployment mode.

    This decorator modifies the table name of a DynamoDB model class
    by appending the deployment mode suffix. For example, if the
    deployment mode is 'development', the table name 'se-configdata'
    becomes 'se-configdata_development'.

    The table name is resolved dynamically at access time, ensuring
    it always reflects the current deployment mode.

    Args:
        cls: The model class to decorate

    Returns:
        The decorated class with dynamic table name resolution
    """
    if not hasattr(cls, "Meta") or not hasattr(cls.Meta, "table_name"):
        return cls

    original_table_name = cls.Meta.table_name

    if not isinstance(original_table_name, str):
        return cls

    class DynamicTableName(str):
        """
        Dynamic table name accessor that resolves table name based on deployment mode.

        This class provides both class-level and instance-level access to the
        table name with proper deployment mode suffix resolution.
        """

        def __init__(self, original: str):
            self._original = str(original).strip()

        def __repr__(self) -> str:
            return self._get_resolved_name()

        def __str__(self) -> str:
            return self._get_resolved_name()

        def __eq__(self, other: Any) -> bool:
            if isinstance(other, DynamicTableName):
                return self._get_resolved_name() == other._get_resolved_name()
            return self._get_resolved_name() == other

        def __hash__(self) -> int:
            return hash(self._get_resolved_name())

        def __bool__(self) -> bool:
            return bool(self._get_resolved_name())

        def __len__(self) -> int:
            return len(self._get_resolved_name())

        def __getitem__(self, key: Any) -> Any:
            return self._get_resolved_name()[key]

        def __add__(self, other: Any) -> str:
            return self._get_resolved_name() + other

        def __radd__(self, other: Any) -> str:
            return other + self._get_resolved_name()

        def __iter__(self):
            return iter(self._get_resolved_name())

        def __format__(self, format_spec: str) -> str:
            return format(self._get_resolved_name(), format_spec)

        def __bytes__(self) -> bytes:
            return self._get_resolved_name().encode("utf-8")

        def __fspath__(self) -> str:
            return self._get_resolved_name()

        def _get_resolved_name(self) -> str:
            regional_deployment = Context.get("regional_deployment")
            print(
                f"Regional deployment (`complete_table_name_decorator`): {regional_deployment}"
            )

            if regional_deployment is not None and not bool(regional_deployment):
                endpoint_id = Context.get("endpoint_id")
                print(f"Endpoint ID (`complete_table_name_decorator`): {endpoint_id}")

                if endpoint_id is not None:
                    return f"{self._original}_{str(endpoint_id).strip().lower()}"

            return self._original

    cls.Meta.table_name = DynamicTableName(original_table_name)

    return cls
