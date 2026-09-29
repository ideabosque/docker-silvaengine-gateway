#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
Base model classes for DynamoDB operations.

This module provides base model classes and mixins for interacting
with DynamoDB, including raw data retrieval functionality.

Example:
    >>> from silvaengine_dynamodb_base import BaseModel, RawDataMixin
    >>>
    >>> class MyModel(RawDataMixin, BaseModel):
    ...     class Meta:
    ...         table_name = 'my_table'
    ...         hash_key = 'id'
    ...     id: UnicodeAttribute = UnicodeAttribute(hash_key=True)
    ...     name: UnicodeAttribute = UnicodeAttribute()
"""

from __future__ import print_function

__author__ = "bibow"

import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Type, TypeVar

from graphene import ObjectType as GraphQLObjectType
from pynamodb.attributes import Attribute
from pynamodb.constants import BOOLEAN, LIST, MAP, NULL, NUMBER, STRING
from pynamodb.exceptions import (
    DoesNotExist,
    GetError,
    PutError,
    QueryError,
    ScanError,
)
from pynamodb.models import Model
from silvaengine_utility import GraphQLTypeGenerator, Serializer, Utility

T = TypeVar("T", bound="BaseModel")

ANY = "ANY"


class AnyAttribute(Attribute[Any]):
    """
    PynamoDB allows for attributes of any type, corresponding to Python's `typing.Any`.

    It supports all Python types: dict, list, str, int, float, nested structures, etc.
    """

    attr_type = ANY

    def serialize(self, value):
        if value is None:
            return {NULL: True}
        elif isinstance(value, bool):
            return {BOOLEAN: value}
        elif isinstance(value, (int, float)):
            return {NUMBER: str(value)}
        elif isinstance(value, str):
            return {STRING: value}
        elif isinstance(value, dict):
            return {MAP: {k: self.serialize(v) for k, v in value.items()}}
        elif isinstance(value, list):
            return {LIST: [self.serialize(v) for v in value]}
        else:
            return {STRING: Serializer.json_dumps(value)}

    def deserialize(self, value):
        if not isinstance(value, dict) or len(value) == 0:
            return None
        type_key, val = next(iter(value.items()))
        if type_key == NULL:
            return None
        elif type_key == BOOLEAN:
            return val
        elif type_key == NUMBER:
            return int(val) if val.isdigit() else float(val)
        elif type_key == STRING:
            return val
        elif type_key == MAP:
            return {k: self.deserialize(v) for k, v in val.items()}
        elif type_key == LIST:
            return [self.deserialize(v) for v in val]
        else:
            return val


class BaseModel(Model):
    """
    Base model class for DynamoDB models.

    This class extends Pynamodb's Model class with custom Meta configuration.
    """

    _DYNAMODB_ATTRIBUTE_TYPES = frozenset(
        {"S", "N", "B", "NULL", "BOOL", "SS", "NS", "BS", "M", "L"}
    )

    _PYTHON_TO_DYNAMODB_TYPE = {
        str: "S",
        int: "N",
        float: "N",
        bool: "BOOL",
        bytes: "B",
        type(None): "NULL",
    }

    class Meta:
        region: Optional[str] = (
            os.getenv("REGION_NAME")
            or os.getenv("REGIONNAME")
            or os.getenv("region_name")
        )
        billing_mode: str = "PAY_PER_REQUEST"
        connect_timeout_seconds = float(os.getenv("CONNECT_TIMEOUT_SECONDS", 10))
        read_timeout_seconds = float(os.getenv("READ_TIMEOUT_SECONDS", 30))
        max_retry_attempts = int(os.getenv("MAX_RETRY_ATTEMPTS", 3))

    @classmethod
    def _get_attribute_metadata(
        cls: Type[T],
    ) -> Dict[str, Dict[str, Any]]:
        """
        Retrieve attribute metadata for the model.

        Returns a dictionary mapping attribute names to their metadata,
        including the attribute type and whether they are hash/range keys.

        Returns:
            Dict mapping attribute names to metadata dictionaries
        """
        if not hasattr(cls, "_attribute_metadata"):
            metadata = {}
            for attr_name, attr_obj in cls.get_attributes().items():
                metadata[attr_name] = {
                    "attr_type": attr_obj.attr_type,
                    "is_hash_key": attr_obj.is_hash_key,
                    "is_range_key": attr_obj.is_range_key,
                    "attr_name": attr_obj.attr_name,
                }
            cls._attribute_metadata = metadata
        return cls._attribute_metadata

    @classmethod
    def _serialize_key_value(
        cls: Type[T],
        value: Any,
        attr_type: str,
    ) -> Dict[str, Any]:
        """
        Serialize a value to DynamoDB attribute format.

        Args:
            value: The value to serialize
            attr_type: The DynamoDB attribute type (S, N, B, etc.)

        Returns:
            A dictionary with the serialized value in DynamoDB format
        """
        if attr_type == "S":
            return {"S": str(value)}
        elif attr_type == "N":
            return {"N": str(value)}
        elif attr_type == "B":
            return {
                "B": value if isinstance(value, bytes) else str(value).encode("utf-8")
            }
        elif attr_type in ("SS", "NS"):
            if isinstance(value, (set, frozenset, list)):
                converted = [str(v) for v in value]
                return {"SS": converted} if attr_type == "SS" else {"NS": converted}
            return {"SS": [str(value)]} if attr_type == "SS" else {"NS": [str(value)]}
        elif attr_type == "BOOL":
            return {"BOOL": bool(value)}
        elif attr_type == "NULL":
            return {"NULL": True}
        else:
            return {"S": str(value)}

    @classmethod
    def _build_key(
        cls: Type[T],
        hash_key: Any,
        range_key: Optional[Any] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Build a DynamoDB key dictionary from hash and range keys.

        This method properly serializes key values according to their
        attribute types, following Pynamodb's attribute serialization patterns.

        Args:
            hash_key: The hash key value
            range_key: The range key value (optional)

        Returns:
            A dictionary representing the DynamoDB key structure

        Example:
            >>> key = cls._build_key('user-123', 'profile')
            >>> # Returns: {'id': {'S': 'user-123'}, 'sort_key': {'S': 'profile'}}
        """
        if not cls._hash_keyname:
            return {}

        attr_metadata = cls._get_attribute_metadata()
        hash_attr_type = attr_metadata.get(cls._hash_keyname, {}).get("attr_type", "S")
        key: Dict[str, Dict[str, Any]] = {
            cls._hash_keyname: cls._serialize_key_value(hash_key, hash_attr_type)
        }

        if range_key is not None and cls._range_keyname is not None:
            range_attr_type = attr_metadata.get(cls._range_keyname, {}).get(
                "attr_type", "S"
            )
            key[cls._range_keyname] = cls._serialize_key_value(
                range_key, range_attr_type
            )

        return key

    @classmethod
    def get_raw(
        cls: Type[T],
        hash_key: Any,
        range_key: Optional[Any] = None,
        consistent_read: bool = False,
        attributes_to_get: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Get raw DynamoDB item data.

        Retrieves a single item from DynamoDB and returns the raw response
        including the item data, consumed capacity, and response metadata.

        Args:
            hash_key: The hash key value of the item to retrieve
            range_key: The range key value (optional, only for composite keys)
            consistent_read: Whether to use strongly consistent read (default: False)

        Returns:
            A dictionary containing:
                - item: The raw DynamoDB item data (dict)
                - consumed_capacity: Capacity units consumed (dict)
                - response_metadata: AWS response metadata (dict)

        Raises:
            DoesNotExist: If the item with the specified key does not exist
            GetError: If unable to retrieve the item from DynamoDB
            Exception: For other AWS-related errors

        Example:
            >>> result = MyModel.get_raw('user-123')
            >>> item = result['item']
        """
        conn = cls._get_connection()

        try:
            response = conn.get_item(
                hash_key=hash_key,
                range_key=range_key,
                consistent_read=consistent_read,
                attributes_to_get=attributes_to_get,
            )

            if "Item" not in response or response["Item"] is None:
                raise DoesNotExist(f"Item with key {hash_key} does not exist")

            return {
                "item": response["Item"],
                "consumed_capacity": response.get("ConsumedCapacity"),
                "response_metadata": response.get("ResponseMetadata"),
            }

        except DoesNotExist:
            raise
        except GetError:
            raise
        except Exception as e:
            raise GetError(f"Failed to get item: {e}") from e

    @classmethod
    def scan_raw(
        cls: Type[T],
        limit: Optional[int] = None,
        filter_expression: Optional[Any] = None,
        expression_attribute_names: Optional[Dict[str, str]] = None,
        expression_attribute_values: Optional[Dict[str, Any]] = None,
        index_name: Optional[str] = None,
        segment: Optional[int] = None,
        total_segments: Optional[int] = None,
        exclusive_start_key: Optional[str] = None,
        consistent_read: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Scan table to get raw data.

        Performs a scan operation on the DynamoDB table and returns
        the raw response including all items and metadata.

        Args:
            limit: Maximum number of items to return (optional)
            filter_expression: Filter condition for the scan (optional)
            expression_attribute_names: Attribute name mappings (optional)
            expression_attribute_values: Attribute value mappings (optional)
            index_name: Name of the index to scan (optional)
            segment: Segment number for parallel scan (optional)
            total_segments: Total number of segments for parallel scan (optional)
            exclusive_start_key: Key to start scanning from (optional)
            consistent_read: Whether to use strongly consistent read (optional)
            **kwargs: Additional parameters passed to the scan operation

        Returns:
            A dictionary containing:
                - items: List of raw DynamoDB items
                - count: Number of items returned
                - scanned_count: Number of items scanned
                - last_evaluated_key: Key for pagination (if applicable)
                - consumed_capacity: Capacity units consumed (dict)
                - response_metadata: AWS response metadata (dict)

        Example:
            >>> result = MyModel.scan_raw(limit=100)
            >>> items = result['items']
        """
        conn = cls._get_connection()

        try:
            response = conn.scan(
                filter_condition=filter_expression,
                limit=limit,
                return_consumed_capacity="TOTAL",
                segment=segment,
                total_segments=total_segments,
                exclusive_start_key=exclusive_start_key,
                consistent_read=consistent_read,
                index_name=index_name,
            )

            return {
                "items": response.get("Items", []),
                "count": response.get("Count", 0),
                "scanned_count": response.get("ScannedCount", 0),
                "last_evaluated_key": response.get("LastEvaluatedKey"),
                "consumed_capacity": response.get("ConsumedCapacity"),
                "response_metadata": response.get("ResponseMetadata"),
            }

        except ScanError:
            raise
        except Exception as e:
            raise ScanError(f"Failed to scan table: {e}") from e

    @classmethod
    def query_raw(
        cls: Type[T],
        hash_key: Any,
        range_key_condition: Optional[Any] = None,
        index_name: Optional[str] = None,
        limit: Optional[int] = None,
        scan_index_forward: Optional[bool] = None,
        filter_expression: Optional[Any] = None,
        exclusive_start_key: Optional[Dict[str, Any]] = None,
        consistent_read: bool = False,
        attributes_to_get: Any | None = None,
        select: str | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Query table to get raw data.

        Performs a query operation on the DynamoDB table and returns
        the raw response including all items and metadata.

        Args:
            hash_key: The hash key value for the query
            range_key_condition: Condition for the range key (optional)
            index_name: Name of the index to query (optional)
            limit: Maximum number of items to return (optional)
            scan_index_forward: Sort order (True for ascending, False for descending)
            filter_expression: Filter condition for the query (optional)
            exclusive_start_key: Key to start querying from (optional)
            consistent_read: Whether to use strongly consistent read (optional)
            **kwargs: Additional parameters passed to the query operation

        Returns:
            A dictionary containing:
                - items: List of raw DynamoDB items
                - count: Number of items returned
                - scanned_count: Number of items scanned
                - last_evaluated_key: Key for pagination (if applicable)
                - consumed_capacity: Capacity units consumed (dict)
                - response_metadata: AWS response metadata (dict)

        Example:
            >>> result = MyModel.query_raw('partition-key', limit=50)
            >>> items = result['items']
        """
        conn = cls._get_connection()

        try:
            return conn.query(
                hash_key=hash_key,
                range_key_condition=range_key_condition,
                filter_condition=filter_expression,
                index_name=index_name,
                limit=limit,
                scan_index_forward=scan_index_forward,
                exclusive_start_key=exclusive_start_key,
                consistent_read=consistent_read,
                return_consumed_capacity="TOTAL",
                attributes_to_get=attributes_to_get,
                select=select,
            )

            # return {
            #     "items": response.get("Items", []),
            #     "count": response.get("Count", 0),
            #     "scanned_count": response.get("ScannedCount", 0),
            #     "last_evaluated_key": response.get("LastEvaluatedKey"),
            #     "consumed_capacity": response.get("ConsumedCapacity"),
            #     "response_metadata": response.get("ResponseMetadata"),
            # }

        except QueryError:
            raise
        except Exception as e:
            raise QueryError(f"Failed to query table: {e}") from e

    @classmethod
    def batch_get_raw(
        cls: Type[T],
        keys: List[Dict[str, Any]],
        consistent_read: bool = False,
        return_consumed_capacity: str = "TOTAL",
    ) -> Dict[str, Any]:
        """
        Batch get raw items from DynamoDB.

        Retrieves multiple items from DynamoDB in a single batch operation.
        This method automatically handles pagination for large result sets.

        Args:
            keys: List of dictionaries containing hash_key and optionally range_key
            consistent_read: Whether to use strongly consistent read (default: False)
            return_consumed_capacity: 'TOTAL', 'INDEXES', or 'NONE' (default: 'TOTAL')

        Returns:
            A dictionary containing:
                - items: List of raw DynamoDB items
                - unprocessed_keys: Keys that were not processed (list)
                - consumed_capacity: Capacity units consumed (dict)
                - response_metadata: AWS response metadata (dict)

        Example:
            >>> keys = [
            ...     {'hash_key': 'item-1'},
            ...     {'hash_key': 'item-2', 'range_key': 'sub-key'}
            ... ]
            >>> result = MyModel.batch_get_raw(keys)
        """
        if not keys:
            return {
                "items": [],
                "unprocessed_keys": [],
                "consumed_capacity": None,
                "response_metadata": None,
            }

        conn = cls._get_connection()

        serialized_keys = []
        for key_dict in keys:
            hash_key = key_dict.get("hash_key")
            range_key = key_dict.get("range_key")
            key = cls._build_key(hash_key, range_key)
            serialized_keys.append(key)

        try:
            response = conn.batch_get_item(
                keys=serialized_keys,
                consistent_read=consistent_read,
                return_consumed_capacity=return_consumed_capacity,
            )

            return {
                "items": response.get("Responses", {}).get(cls.Meta.table_name, []),
                "unprocessed_keys": response.get("UnprocessedKeys", []),
                "consumed_capacity": response.get("ConsumedCapacity"),
                "response_metadata": response.get("ResponseMetadata"),
            }

        except GetError:
            raise
        except Exception as e:
            raise GetError(f"Failed to batch get items: {e}") from e

    @classmethod
    @contextmanager
    def batch_write_raw(
        cls: Type[T],
        return_consumed_capacity: str = "TOTAL",
        return_item_collection_metrics: str = "NONE",
    ) -> Iterator[Dict[str, Any]]:
        """
        Context manager for batch write operations.

        Provides a convenient interface for performing batch write operations
        with automatic commit on context exit.

        Args:
            return_consumed_capacity: 'TOTAL', 'INDEXES', or 'NONE'
            return_item_collection_metrics: 'SIZE' or 'NONE'

        Yields:
            A dictionary with 'put_items' and 'delete_items' lists

        Example:
            >>> with MyModel.batch_write_raw() as batch:
            ...     batch['put_items'].append({'id': {'S': 'new-item'}})
            ...     batch['delete_items'].append({'id': {'S': 'old-item'}})
        """
        conn = cls._get_connection()

        pending = {"put_items": [], "delete_items": []}
        result = {}

        try:
            yield pending

            put_items = [item for item in pending["put_items"] if item is not None]
            delete_items = [
                item for item in pending["delete_items"] if item is not None
            ]

            if put_items or delete_items:
                result = conn.batch_write_item(
                    put_items=put_items,
                    delete_items=delete_items,
                    return_consumed_capacity=return_consumed_capacity,
                    return_item_collection_metrics=return_item_collection_metrics,
                )

            return result
        except PutError as e:
            raise PutError(f"Failed to put items in batch write: {e}") from e
        except Exception:
            raise

    @classmethod
    def get_items_batch(
        cls: Type[T],
        keys: List[Dict[str, Any]],
        consistent_read: bool = False,
        chunksize: int = 100,
    ) -> Iterator[Dict[str, Any]]:
        """
        Generator that yields batch get results.

        Automatically handles pagination for large key sets by yielding
        results in chunks of up to 'chunksize' items.

        Args:
            keys: List of dictionaries containing hash_key and optionally range_key
            consistent_read: Whether to use strongly consistent read
            chunksize: Maximum number of keys per batch (default: 100)

        Yields:
            Batch result dictionaries containing items and metadata

        Example:
            >>> for batch in MyModel.get_items_batch(large_key_list):
            ...     for item in batch['items']:
            ...         process(item)
        """
        if not isinstance(keys, list):
            raise TypeError(f"keys must be a list, not {type(keys).__name__}")

        if not keys:
            return

        # conn = cls._get_connection()

        for i in range(0, len(keys), chunksize):
            chunk = keys[i : i + chunksize]

            result = cls.batch_get_raw(chunk, consistent_read=consistent_read)
            yield result

            while result.get("unprocessed_keys"):
                retry_keys = result["unprocessed_keys"]
                result = cls.batch_get_raw(retry_keys, consistent_read=consistent_read)
                yield result

    @classmethod
    def scan_paginated(
        cls: Type[T],
        limit: Optional[int] = None,
        filter_expression: Optional[Any] = None,
        segment: Optional[int] = None,
        total_segments: Optional[int] = None,
        **kwargs: Any,
    ) -> Iterator[Dict[str, Any]]:
        """
        Generator that yields scan results with automatic pagination.

        Handles pagination automatically by yielding items from all pages
        until the limit is reached or the scan is complete.

        Args:
            limit: Maximum total items to return (optional)
            filter_expression: Filter condition for the scan (optional)
            segment: Segment number for parallel scan (optional)
            total_segments: Total number of segments for parallel scan (optional)
            **kwargs: Additional scan parameters

        Yields:
            Item dictionaries from the scan results

        Example:
            >>> for item in MyModel.scan_paginated(limit=1000):
            ...     process(item)
        """
        conn = cls._get_connection()
        exclusive_start_key = None
        total_count = 0

        while limit is None or total_count < limit:
            response = conn.scan(
                filter_condition=filter_expression,
                limit=limit - total_count if limit else None,
                exclusive_start_key=exclusive_start_key,
                segment=segment,
                total_segments=total_segments,
                return_consumed_capacity="TOTAL",
            )

            items = response.get("Items", [])
            for item in items:
                if limit and total_count >= limit:
                    break
                yield item
                total_count += 1

            exclusive_start_key = response.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break

    @classmethod
    def query_paginated(
        cls: Type[T],
        hash_key: Any,
        range_key_condition: Optional[Any] = None,
        index_name: Optional[str] = None,
        limit: Optional[int] = None,
        scan_index_forward: Optional[bool] = None,
        filter_expression: Optional[Any] = None,
        consistent_read: bool = False,
        **kwargs: Any,
    ) -> Iterator[Dict[str, Any]]:
        """
        Generator that yields query results with automatic pagination.

        Handles pagination automatically by yielding items from all pages
        until the limit is reached or the query is complete.

        Args:
            hash_key: The hash key value for the query
            range_key_condition: Condition for the range key (optional)
            index_name: Name of the index to query (optional)
            limit: Maximum total items to return (optional)
            scan_index_forward: Sort order (True for ascending, False for descending)
            filter_expression: Filter condition for the query (optional)
            consistent_read: Whether to use strongly consistent read
            **kwargs: Additional query parameters

        Yields:
            Item dictionaries from the query results

        Example:
            >>> for item in MyModel.query_paginated('partition-key', limit=100):
            ...     process(item)
        """
        conn = cls._get_connection()
        exclusive_start_key = None
        total_count = 0

        while limit is None or total_count < limit:
            response = conn.query(
                hash_key=hash_key,
                range_key_condition=range_key_condition,
                index_name=index_name,
                limit=limit - total_count if limit else None,
                scan_index_forward=scan_index_forward,
                filter_condition=filter_expression,
                exclusive_start_key=exclusive_start_key,
                consistent_read=consistent_read,
                return_consumed_capacity="TOTAL",
            )

            items = response.get("Items", [])
            for item in items:
                if limit and total_count >= limit:
                    break
                yield item
                total_count += 1

            exclusive_start_key = response.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break

    @staticmethod
    def _get_attribute_type(value: Any) -> Optional[str]:
        """
        Determine the DynamoDB attribute type for a Python value.

        Uses optimized type mapping for fast lookups.

        Args:
            value: The Python value to check

        Returns:
            The DynamoDB attribute type string (S, N, B, SS, NS, BS, M, L, NULL, BOOL)
            or None if type cannot be determined
        """
        if value is None:
            return "NULL"

        value_type = type(value)
        type_map = BaseModel._PYTHON_TO_DYNAMODB_TYPE

        if value_type in type_map:
            attr_type = type_map[value_type]
            if attr_type != "NULL":
                return attr_type

        if value_type is list:
            return "L"
        elif value_type is dict:
            return "M"
        elif value_type in (set, frozenset):
            if value:
                first_item = next(iter(value))
                return "SS" if isinstance(first_item, str) else "NS"
            return "SS"

        return "S"

    @classmethod
    def _get_value_from_raw(
        cls,
        raw_data: Optional[Dict[str, Any]],
        attribute_type: str,
    ) -> Any:
        attribute_type = str(attribute_type).strip()

        if type(raw_data) is not dict or not attribute_type:
            return raw_data

        return raw_data.get(attribute_type)

    @classmethod
    def _get_model_attributes(cls) -> Dict[str, Any]:
        """
        Get all attribute definitions from the model class.

        Returns:
            Dictionary mapping attribute names to attribute objects
        """
        if not hasattr(cls, "_model_attributes_cache"):
            attributes = {}

            for attr_name in dir(cls):
                attr_obj = getattr(cls, attr_name, None)

                if attr_obj is not None and hasattr(attr_obj, "attr_type"):
                    attributes[attr_name] = attr_obj
            cls._model_attributes_cache = attributes
        return cls._model_attributes_cache

    @classmethod
    def _get_dynamodb_value(cls, value: Any, attr_type: str) -> Any:
        """
        Parse DynamoDB attribute value based on attribute type.

        This method deserializes DynamoDB attribute values with type annotations
        into their corresponding Python types. It handles all DynamoDB attribute
        types including nested structures (M, L) with recursive processing.

        Args:
            value: The DynamoDB attribute value dictionary
                Format: {'S': 'string'}, {'N': '42'}, {'M': {...}}, etc.
            attr_type: The DynamoDB attribute type (S, N, B, SS, NS, BS, M, L, NULL, BOOL)

        Returns:
            The deserialized Python value

        Raises:
            ValueError: If the value format is invalid or cannot be parsed
            KeyError: If the expected attribute type key is not found in value
        """
        if not isinstance(value, dict):
            return value

        if attr_type not in value:
            raise KeyError(
                f"Attribute type '{attr_type}' not found in value. "
                f"Available types: {list(value.keys())}"
            )

        parsed_value = value.get(attr_type)

        if parsed_value is None or attr_type == "NULL":
            return None
        elif attr_type == "S":
            return str(parsed_value)
        elif attr_type == "N":
            if isinstance(parsed_value, (int, float)):
                return parsed_value
            return (
                int(parsed_value)
                if "." not in str(parsed_value)
                else float(parsed_value)
            )
        elif attr_type == "B":
            if isinstance(parsed_value, bytes):
                return parsed_value
            return (
                parsed_value.encode("utf-8")
                if isinstance(parsed_value, str)
                else bytes(parsed_value)
            )
        elif attr_type == "BOOL":
            return bool(parsed_value)
        elif attr_type == "SS":
            if isinstance(parsed_value, (set, frozenset, list)):
                return set(str(v) for v in parsed_value)
            return {str(parsed_value)}
        elif attr_type == "NS":
            if isinstance(parsed_value, (set, frozenset, list)):
                result = set()
                for v in parsed_value:
                    result.add(int(v) if "." not in str(v) else float(v))
                return result
            return {int(parsed_value)}
        elif attr_type == "BS":
            if isinstance(parsed_value, (set, frozenset, list)):
                return set(
                    v.encode("utf-8") if isinstance(v, str) else v for v in parsed_value
                )
            return {
                parsed_value.encode("utf-8")
                if isinstance(parsed_value, str)
                else parsed_value
            }
        elif attr_type == "M":
            if not isinstance(parsed_value, dict):
                return {"value": parsed_value}
            result = {}
            for k, v in parsed_value.items():
                if isinstance(v, dict):
                    inner_type = next(
                        (t for t in cls._DYNAMODB_ATTRIBUTE_TYPES if t in v), None
                    )
                    result[k] = (
                        cls._get_dynamodb_value(v, inner_type) if inner_type else v
                    )
                else:
                    result[k] = v
            return result
        elif attr_type == "L":
            if not isinstance(parsed_value, list):
                return [parsed_value]
            result = []
            for item in parsed_value:
                if isinstance(item, dict):
                    inner_type = next(
                        (t for t in cls._DYNAMODB_ATTRIBUTE_TYPES if t in item), None
                    )
                    result.append(
                        cls._get_dynamodb_value(item, inner_type)
                        if inner_type
                        else item
                    )
                else:
                    result.append(item)
            return result

        return parsed_value

    @classmethod
    def boto3_item_to_model(
        cls,
        boto3_item: Dict[str, Any],
        raise_on_missing: bool = True,
    ) -> "BaseModel":
        """
        Convert a boto3 DynamoDB item to a pynamodb Model instance.

        This method takes the raw item format returned by boto3 DynamoDB queries
        (where values are plain Python types without type annotations) and converts
        it to a pynamodb Model instance with properly typed attribute values.

        Args:
            boto3_item: Dictionary representing a DynamoDB item as returned by boto3.
                Format: {'attribute_name': value, ...}
                Example: {'setting_id': 'test', 'variable': 'KEY', 'value': 'data'}
            raise_on_missing: Whether to raise ValueError if required fields are missing.
                If False, missing fields are set to None. (default: True)

        Returns:
            An instance of the calling model class with attributes populated

        Raises:
            TypeError: If boto3_item is not a dictionary
            ValueError: If required fields are missing and raise_on_missing is True
            AttributeError: If model class doesn't define an attribute

        Example:
            >>> boto3_item = {
            ...     'setting_id': 'beta_core_gpt',
            ...     'variable': 'OPENAI_API_KEY',
            ...     'value': 'sk-abc123xyz'
            ... }
            >>> model = ConfigModel.boto3_item_to_model(boto3_item)
            >>> print(model.setting_id, model.variable, model.value)
            'beta_core_gpt' 'OPENAI_API_KEY' 'sk-abc123xyz'
        """
        if not isinstance(boto3_item, dict):
            raise TypeError(
                f"boto3_item must be a dict, got {type(boto3_item).__name__}"
            )

        model_attrs = cls._get_model_attributes()
        model = cls()
        required_fields = set()

        for attr_name, attr_obj in model_attrs.items():
            if attr_obj.is_hash_key:
                required_fields.add(attr_name)
            if attr_obj.is_range_key:
                required_fields.add(attr_name)

        missing_fields = required_fields - set(boto3_item.keys())

        if missing_fields and raise_on_missing:
            raise ValueError(
                f"Missing required fields for model {cls.__name__}: {missing_fields}"
            )

        for attr_name, attr_obj in model_attrs.items():
            if attr_name in boto3_item:
                attr_type = attr_obj.attr_type
                raw_value = boto3_item.get(attr_name)

                try:
                    if raw_value is None:
                        setattr(model, attr_name, None)
                    elif attr_type == ANY:
                        if isinstance(raw_value, dict):
                            value = next(
                                (
                                    cls._get_dynamodb_value(raw_value, t)
                                    for t in cls._DYNAMODB_ATTRIBUTE_TYPES
                                    if t in raw_value
                                ),
                                None,
                            )

                            setattr(model, attr_name, value)
                    else:
                        setattr(
                            model,
                            attr_name,
                            cls._get_value_from_raw(raw_value, attr_type),
                        )
                except (TypeError, ValueError) as e:
                    raise ValueError(
                        f"Failed to set attribute '{attr_name}' with value {raw_value}: {e}"
                    ) from e
            elif raise_on_missing or attr_obj.is_hash_key or attr_obj.is_range_key:
                setattr(model, attr_name, None)

        return model

    @classmethod
    def boto3_items_to_models(
        cls,
        boto3_items: List[Dict[str, Any]],
        raise_on_missing: bool = True,
    ) -> List["BaseModel"]:
        """
        Convert a list of boto3 DynamoDB items to pynamodb Model instances.

        Args:
            boto3_items: List of dictionaries representing DynamoDB items
            raise_on_missing: Whether to raise ValueError if required fields are missing

        Returns:
            List of model instances

        Raises:
            TypeError: If boto3_items is not a list
            ValueError: If required fields are missing
            AttributeError: If model class doesn't define an attribute

        Example:
            >>> items = [
            ...     {'setting_id': 'test', 'variable': 'KEY1', 'value': 'val1'},
            ...     {'setting_id': 'test', 'variable': 'KEY2', 'value': 'val2'}
            ... ]
            >>> models = ConfigModel.boto3_items_to_models(items)
            >>> for m in models:
            ...     print(m.variable, m.value)
        """
        if boto3_items is None:
            return []

        if not isinstance(boto3_items, list):
            raise TypeError(
                f"boto3_items must be a list, got {type(boto3_items).__name__}"
            )

        models = []

        for index, item in enumerate(boto3_items):
            if not isinstance(item, dict):
                raise TypeError(
                    f"Item at index {index} must be a dict, got {type(item).__name__}"
                )
            try:
                model = cls.boto3_item_to_model(item, raise_on_missing=raise_on_missing)
                models.append(model)
            except (ValueError, AttributeError) as e:
                raise ValueError(f"Failed to convert item at index {index}: {e}") from e

        return models

    @classmethod
    def model_to_boto3_item(
        cls,
        model: "BaseModel",
        include_none: bool = False,
        return_python_dict: bool = False,
    ) -> Dict[str, Any]:
        """
        Convert a pynamodb Model instance to boto3 DynamoDB item format or Python dict.

        This method takes a pynamodb Model instance and converts it to either:
        - boto3 DynamoDB format with type annotations (default)
        - Python dict format (when return_python_dict=True)

        Args:
            model: A pynamodb Model instance to convert
            include_none: Whether to include None values in the output (default: False)
            return_python_dict: If True, returns plain Python dict without DynamoDB
                type annotations. If False (default), returns boto3 format with
                type annotations like {'S': 'value'}, {'N': '123'}, etc.

        Returns:
            If return_python_dict is False (default):
                Dictionary representing the item in boto3 format with proper type annotations.
                Format: {'attribute_name': {'type': value}, ...}
            If return_python_dict is True:
                Dictionary with plain Python values.
                Format: {'attribute_name': value, ...}

        Raises:
            TypeError: If model is not a pynamodb Model instance
            ValueError: If model contains invalid ANY type data

        Example:
            >>> model = ConfigModel()
            >>> model.setting_id = 'beta_core_gpt'
            >>> model.variable = 'OPENAI_API_KEY'
            >>> model.value = 'sk-abc123xyz'
            >>>
            >>> # Get boto3 format (default)
            >>> item = ConfigModel.model_to_boto3_item(model)
            >>> # Returns: {'setting_id': {'S': 'beta_core_gpt'}, ...}
            >>>
            >>> # Get plain dict format
            >>> item = ConfigModel.model_to_boto3_item(model, return_python_dict=True)
            >>> # Returns: {'setting_id': 'beta_core_gpt', ...}
        """
        if not isinstance(model, Model):
            raise TypeError(
                f"model must be a pynamodb Model instance, got {type(model).__name__}"
            )

        model_attrs = cls._get_model_attributes()
        result: Dict[str, Any] = {}

        for attr_name, attr_obj in model_attrs.items():
            if not hasattr(model, attr_name):
                continue

            value = getattr(model, attr_name, None)

            if value is None and not include_none:
                continue

            attr_type = attr_obj.attr_type
            result[attr_name] = cls._serialize_attribute_value(
                value, attr_type, return_python_dict
            )

        return result

    @classmethod
    def models_to_boto3_items(
        cls,
        models: List["BaseModel"],
        include_none: bool = False,
        return_python_dict: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Convert a list of pynamodb Model instances to boto3 DynamoDB item format.

        Args:
            models: List of pynamodb Model instances
            include_none: Whether to include None values in the output
            return_python_dict: Whether to return plain Python dict format

        Returns:
            List of dictionaries in boto3 format

        Raises:
            TypeError: If models is not a list or contains non-Model instances

        Example:
            >>> models = [model1, model2, model3]
            >>> items = ConfigModel.models_to_boto3_items(models)
        """
        if models is None:
            return []

        if not isinstance(models, list):
            raise TypeError(f"models must be a list, got {type(models).__name__}")

        items = []
        for index, model in enumerate(models):
            if not isinstance(model, Model):
                raise TypeError(
                    f"Model at index {index} must be a pynamodb Model instance, "
                    f"got {type(model).__name__}"
                )
            items.append(
                cls.model_to_boto3_item(
                    model,
                    include_none=include_none,
                    return_python_dict=return_python_dict,
                )
            )

        return items

    @classmethod
    def _serialize_attribute_value(
        cls,
        value: Any,
        attr_type: str,
        return_python_dict: bool = False,
    ) -> Any:
        """
        Serialize a single attribute value based on its type.

        Args:
            value: The attribute value to serialize
            attr_type: The DynamoDB attribute type
            return_python_dict: Whether to return plain Python dict

        Returns:
            Serialized value in the appropriate format
        """
        if value is None:
            return None if return_python_dict else {"NULL": True}

        if attr_type == ANY:
            try:
                serialized = cls._serialize_any_value(value, return_python_dict)
                return serialized if return_python_dict else {"ANY": serialized}
            except (TypeError, ValueError) as e:
                raise ValueError(f"Failed to serialize ANY attribute: {e}") from e

        if return_python_dict:
            return cls._serialize_to_python(value, attr_type)
        else:
            return cls._serialize_to_boto3(value, attr_type)

    @classmethod
    def _serialize_to_python(cls, value: Any, attr_type: str) -> Any:
        """
        Serialize value to plain Python type.

        Args:
            value: The value to serialize
            attr_type: The DynamoDB attribute type

        Returns:
            Plain Python value
        """
        if attr_type == "S":
            return str(value)
        elif attr_type == "N":
            if isinstance(value, (int, float)):
                return value
            return float(value) if "." in str(value) else int(value)
        elif attr_type == "B":
            if isinstance(value, bytes):
                return value
            return value.encode("utf-8")
        elif attr_type == "BOOL":
            return bool(value)
        elif attr_type in ("SS", "NS"):
            converted = (
                [str(v) for v in value]
                if isinstance(value, (set, frozenset, list))
                else [value]
            )
            return (
                set(converted)
                if attr_type == "SS"
                else {float(v) if "." in str(v) else int(v) for v in converted}
            )
        elif attr_type == "BS":
            converted = (
                list(value) if isinstance(value, (set, frozenset, list)) else [value]
            )
            return set(converted)
        elif attr_type == "M":
            if isinstance(value, dict):
                return cls._serialize_dict_value(value, return_python_dict=True)
            return {"value": value}
        elif attr_type == "L":
            if isinstance(value, list):
                return cls._serialize_list_value(value, return_python_dict=True)
            return [value]
        elif attr_type == "NULL":
            return None
        else:
            return str(value)

    @classmethod
    def _serialize_to_boto3(cls, value: Any, attr_type: str) -> Dict[str, Any]:
        """
        Serialize value to boto3 DynamoDB format with type annotations.

        Args:
            value: The value to serialize
            attr_type: The DynamoDB attribute type

        Returns:
            Dictionary with type annotation
        """
        if attr_type == "S":
            return {"S": str(value)}
        elif attr_type == "N":
            return {"N": str(value)}
        elif attr_type == "B":
            if isinstance(value, bytes):
                return {"B": value}
            return {"B": str(value).encode("utf-8")}
        elif attr_type == "BOOL":
            return {"BOOL": bool(value)}
        elif attr_type == "SS":
            converted = (
                [str(v) for v in value]
                if isinstance(value, (set, frozenset, list))
                else [str(value)]
            )
            return {"SS": converted}
        elif attr_type == "NS":
            converted = (
                [str(v) for v in value]
                if isinstance(value, (set, frozenset, list))
                else [str(value)]
            )
            return {"NS": converted}
        elif attr_type == "BS":
            converted = (
                list(value) if isinstance(value, (set, frozenset, list)) else [value]
            )
            return {"BS": converted}
        elif attr_type == "M":
            if isinstance(value, dict):
                return {"M": cls._serialize_dict_value(value, return_python_dict=False)}
            return {"M": {"value": value}}
        elif attr_type == "L":
            if isinstance(value, list):
                return {"L": cls._serialize_list_value(value, return_python_dict=False)}
            return {"L": [value]}
        elif attr_type == "NULL":
            return {"NULL": True}
        else:
            return {"S": str(value)}

    @classmethod
    def _serialize_any_value(
        cls,
        value: Any,
        return_python_dict: bool = False,
    ) -> Any:
        """
        Serialize ANY type value to boto3 format or Python dict.

        Handles complex nested structures including:
        - Primitive types (str, int, float, bool, None)
        - Collections (dict, list, set, frozenset)
        - Nested combinations of above

        Args:
            value: The value to serialize
            return_python_dict: Whether to return plain Python dict

        Returns:
            Serialized value in the appropriate format

        Raises:
            TypeError: If value contains unsupported types
            ValueError: If serialization fails
        """
        if value is None:
            return None

        value_type = type(value)

        if value_type is str:
            return value if return_python_dict else {"S": value}
        elif value_type is bool:
            return value if return_python_dict else {"BOOL": value}
        elif value_type in (int, float):
            num_str = str(value)
            return value if return_python_dict else {"N": num_str}
        elif value_type is dict:
            return cls._serialize_dict_value(value, return_python_dict)
        elif value_type is list:
            return cls._serialize_list_value(value, return_python_dict)
        elif value_type in (set, frozenset):
            return cls._serialize_set_value(value, return_python_dict)
        elif value_type is bytes:
            encoded = value if return_python_dict else {"B": value}
            return encoded if return_python_dict else encoded
        else:
            try:
                json_str = Serializer.json_dumps(value)
                return json_str if return_python_dict else {"S": json_str}
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"Unsupported type for ANY attribute: {value_type.__name__}"
                ) from e

    @classmethod
    def _serialize_dict_value(
        cls,
        value: Dict[str, Any],
        return_python_dict: bool = False,
    ) -> Dict[str, Any]:
        """
        Serialize dict value to boto3 format or Python dict.

        Args:
            value: The dict to serialize
            return_python_dict: Whether to return plain Python dict

        Returns:
            Serialized dict
        """
        if not isinstance(value, dict):
            if return_python_dict:
                return {"value": value}
            else:
                serialized = cls._serialize_any_value(value, return_python_dict)
                return {"value": serialized}

        result = {}
        for k, v in value.items():
            if isinstance(v, dict):
                result[k] = cls._serialize_dict_value(v, return_python_dict)
            elif isinstance(v, list):
                result[k] = cls._serialize_list_value(v, return_python_dict)
            elif isinstance(v, (set, frozenset)):
                result[k] = cls._serialize_set_value(v, return_python_dict)
            else:
                serialized = cls._serialize_any_value(v, return_python_dict)
                if return_python_dict:
                    result[k] = serialized
                elif isinstance(serialized, dict) and len(serialized) == 1:
                    result[k] = serialized
                else:
                    result[k] = serialized

        return result if return_python_dict else {"M": result}

    @classmethod
    def _serialize_list_value(
        cls,
        value: List[Any],
        return_python_dict: bool = False,
    ) -> List[Any]:
        """
        Serialize list value to boto3 format or Python list.

        Args:
            value: The list to serialize
            return_python_dict: Whether to return plain Python list

        Returns:
            Serialized list
        """
        if not isinstance(value, list):
            serialized = cls._serialize_any_value(value, return_python_dict)
            return [serialized] if return_python_dict else [serialized]

        result = []
        for item in value:
            if isinstance(item, dict):
                result.append(cls._serialize_dict_value(item, return_python_dict))
            elif isinstance(item, list):
                result.append(cls._serialize_list_value(item, return_python_dict))
            elif isinstance(item, (set, frozenset)):
                result.append(cls._serialize_set_value(item, return_python_dict))
            else:
                serialized = cls._serialize_any_value(item, return_python_dict)
                result.append(serialized)

        return result if return_python_dict else {"L": result}

    @classmethod
    def _serialize_set_value(
        cls,
        value: Any,
        return_python_dict: bool = False,
    ) -> Any:
        """
        Serialize set value to boto3 format or Python set.

        Args:
            value: The set to serialize
            return_python_dict: Whether to return plain Python set

        Returns:
            Serialized set
        """
        if not isinstance(value, (set, frozenset)):
            if return_python_dict:
                return {value}
            else:
                serialized = cls._serialize_any_value(value, return_python_dict)
                if isinstance(serialized, dict) and "S" in serialized:
                    return {"SS": [serialized["S"]]}
                return {"SS": [str(value)]}

        items_list = list(value)
        if not items_list:
            return set() if return_python_dict else {"SS": []}

        first_item = items_list[0]
        first_type = type(first_item)

        if first_type is str:
            serialized = [str(v) for v in items_list]
            return set(serialized) if return_python_dict else {"SS": serialized}
        elif first_type in (int, float):
            serialized = [str(v) for v in items_list]
            return (
                {float(v) if "." in str(v) else int(v) for v in items_list}
                if return_python_dict
                else {"NS": serialized}
            )
        else:
            serialized = [
                cls._serialize_any_value(v, return_python_dict) for v in items_list
            ]
            str_items = [str(v) for v in serialized]
            return set(str_items) if return_python_dict else {"SS": str_items}

    @classmethod
    def serialize_model_attribute(
        cls,
        model: "BaseModel",
        attr_name: str,
    ) -> Dict[str, Any]:
        """
        Serialize a single model attribute to boto3 DynamoDB format.

        Args:
            model: The pynamodb Model instance
            attr_name: The name of the attribute to serialize

        Returns:
            Dictionary with DynamoDB type annotation

        Raises:
            AttributeError: If attribute doesn't exist on the model
        """
        if not hasattr(model, attr_name):
            raise AttributeError(
                f"Model {type(model).__name__} has no attribute '{attr_name}'"
            )

        value = getattr(model, attr_name, None)
        model_attrs = cls._get_model_attributes()

        if attr_name not in model_attrs:
            raise AttributeError(f"'{attr_name}' is not a defined DynamoDB attribute")

        attr_obj = model_attrs[attr_name]
        attr_type = attr_obj.attr_type

        if value is None:
            return {"NULL": True}
        elif attr_type == "S":
            return {"S": str(value)}
        elif attr_type == "N":
            return {"N": str(value)}
        elif attr_type == "B":
            if isinstance(value, bytes):
                return {"B": value}
            return {"B": str(value).encode("utf-8")}
        elif attr_type == "BOOL":
            return {"BOOL": bool(value)}
        elif attr_type in ("SS", "NS", "BS"):
            if isinstance(value, (set, frozenset)):
                if attr_type == "SS":
                    return {"SS": [str(v) for v in value]}
                elif attr_type == "NS":
                    return {"NS": [str(v) for v in value]}
                else:
                    return {"BS": list(value)}
            return {"SS": [str(value)]}
        elif attr_type == "M":
            return {"M": value} if isinstance(value, dict) else {"M": {"value": value}}
        elif attr_type == "L":
            return {"L": value} if isinstance(value, list) else {"L": [value]}
        elif attr_type == "NULL":
            return {"NULL": True}
        else:
            return {"S": str(value)}

    @classmethod
    def build_key_condition(
        cls,
        hash_key_value: Any,
        range_key_value: Optional[Any] = None,
        range_key_name: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Build a DynamoDB key condition dictionary for boto3 operations.

        Args:
            hash_key_value: The hash key value
            range_key_value: The range key value (optional)
            range_key_name: The name of the range key attribute

        Returns:
            Dictionary with hash_key and optionally range_key in boto3 format

        Example:
            >>> key = ConfigModel.build_key_condition('test-setting', 'key-name', 'key')
            >>> # Returns:
            >>> # {
            >>> #     'setting_id': {'S': 'test-setting'},
            >>> #     'key': {'S': 'key-name'}
            >>> # }
        """
        model_attrs = cls._get_model_attributes()

        hash_key_name = None
        for attr_name, attr_obj in model_attrs.items():
            if attr_obj.is_hash_key:
                hash_key_name = attr_name
                break

        if not hash_key_name:
            raise ValueError("Model does not define a hash key")

        key_condition = {}

        hash_attr_type = model_attrs[hash_key_name].attr_type
        if hash_attr_type == "S":
            key_condition[hash_key_name] = {"S": str(hash_key_value)}
        elif hash_attr_type == "N":
            key_condition[hash_key_name] = {"N": str(hash_key_value)}
        else:
            key_condition[hash_key_name] = {"S": str(hash_key_value)}

        if range_key_value is not None and range_key_name:
            range_attr = model_attrs.get(range_key_name)
            if range_attr:
                range_attr_type = range_attr.attr_type
                if range_attr_type == "S":
                    key_condition[range_key_name] = {"S": str(range_key_value)}
                elif range_attr_type == "N":
                    key_condition[range_key_name] = {"N": str(range_key_value)}
                else:
                    key_condition[range_key_name] = {"S": str(range_key_value)}

        return key_condition

    @classmethod
    def boto3_item_to_dict(
        cls,
        boto3_item: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Convert a boto3 DynamoDB item to a plain Python dictionary.

        This method takes a boto3 DynamoDB item (which may contain values in
        DynamoDB format with type annotations like {'S': 'value'}, {'N': '123'})
        and converts it to a plain Python dictionary with native Python types.
        This is useful when you need to work with the raw data without type annotations.

        Args:
            boto3_item: Dictionary representing a DynamoDB item.
                Format can be either:
                - With type annotations: {'attribute': {'S': 'value'}, ...}
                - Without type annotations: {'attribute': 'value', ...}
                Example:
                    {'setting_id': {'S': 'test'}, 'value': {'N': '42'}}
                    or
                    {'setting_id': 'test', 'value': 42}

        Returns:
            A plain Python dictionary with native Python types.
            Format: {'attribute_name': value, ...}
            All DynamoDB type annotations are removed and values are converted
            to their corresponding Python types (str, int, float, bool, list, dict, set).

        Raises:
            TypeError: If boto3_item is not a dictionary
            ValueError: If the item contains invalid type annotations

        Example:
            >>> boto3_item = {
            ...     'setting_id': {'S': 'beta_core_gpt'},
            ...     'variable': {'S': 'OPENAI_API_KEY'},
            ...     'value': {'S': 'sk-abc123xyz'}
            ... }
            >>> result = ConfigModel.boto3_item_to_dict(boto3_item)
            >>> # Returns:
            >>> # {
            >>> #     'setting_id': 'beta_core_gpt',
            >>> #     'variable': 'OPENAI_API_KEY',
            >>> #     'value': 'sk-abc123xyz'
            >>> # }

            >>> # Mixed format also supported
            >>> boto3_item = {'setting_id': 'test', 'count': {'N': '42'}}
            >>> result = ConfigModel.boto3_item_to_dict(boto3_item)
            >>> # Returns: {'setting_id': 'test', 'count': 42}
        """
        if not isinstance(boto3_item, dict):
            raise TypeError(
                f"boto3_item must be a dictionary, got {type(boto3_item).__name__}"
            )

        result: Dict[str, Any] = {}

        for attr_name, attr_value in boto3_item.items():
            result[attr_name] = cls._convert_dynamodb_value(attr_value)

        return result

    @classmethod
    def _convert_dynamodb_value(cls, value: Any) -> Any:
        """
        Convert a DynamoDB value to its corresponding Python type.

        This internal method handles the conversion of DynamoDB attribute values
        (which may have type annotations) to native Python types.

        Args:
            value: The value to convert. Can be:
                - A DynamoDB typed value: {'S': 'text'}, {'N': '42'}
                - A plain Python value: 'text', 42, True, etc.
                - A collection: {'L': [...]}, {'M': {...}}

        Returns:
            The converted Python value with appropriate type
        """
        if value is None:
            return None

        if not isinstance(value, dict):
            return value

        if len(value) == 0:
            return value

        type_key, type_value = next(iter(value.items()))

        if type_key == "NULL":
            return None
        elif type_key == "S":
            return str(type_value)
        elif type_key == "N":
            if isinstance(type_value, (int, float)):
                return type_value
            return int(type_value) if "." not in str(type_value) else float(type_value)
        elif type_key == "B":
            if isinstance(type_value, bytes):
                return type_value
            return (
                type_value.encode("utf-8")
                if isinstance(type_value, str)
                else bytes(type_value)
            )
        elif type_key == "BOOL":
            return bool(type_value)
        elif type_key == "SS":
            if isinstance(type_value, (set, frozenset, list)):
                return set(str(v) for v in type_value)
            return {str(type_value)}
        elif type_key == "NS":
            if isinstance(type_value, (set, frozenset, list)):
                result = set()
                for v in type_value:
                    result.add(int(v) if "." not in str(v) else float(v))
                return result
            return {int(type_value)}
        elif type_key == "BS":
            if isinstance(type_value, (set, frozenset, list)):
                return set(
                    v.encode("utf-8") if isinstance(v, str) else v for v in type_value
                )
            encoded = (
                type_value.encode("utf-8")
                if isinstance(type_value, str)
                else type_value
            )
            return {encoded}
        elif type_key == "M":
            if not isinstance(type_value, dict):
                return {"value": type_value}
            converted = {}
            for k, v in type_value.items():
                converted[k] = cls._convert_dynamodb_value(v)
            return converted
        elif type_key == "L":
            if not isinstance(type_value, list):
                return [type_value]
            return [cls._convert_dynamodb_value(item) for item in type_value]
        else:
            return type_value

    @classmethod
    def boto3_items_to_dict_list(
        cls,
        boto3_items: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Convert a list of boto3 DynamoDB items to a list of plain Python dictionaries.

        This method takes a list of boto3 DynamoDB items and converts each item
        to a plain Python dictionary with native Python types. This is useful
        for batch processing of DynamoDB query results.

        Args:
            boto3_items: List of dictionaries representing DynamoDB items.
                Each item can have values with or without type annotations.
                Example:
                    [
                        {'setting_id': {'S': 'test1'}, 'value': {'N': '1'}},
                        {'setting_id': {'S': 'test2'}, 'value': {'N': '2'}}
                    ]

        Returns:
            A list of plain Python dictionaries with native Python types.
            Format: [{'attribute': value, ...}, ...]

        Raises:
            TypeError: If boto3_items is not a list
            TypeError: If any item in the list is not a dictionary

        Example:
            >>> boto3_items = [
            ...     {
            ...         'setting_id': {'S': 'config1'},
            ...         'variable': {'S': 'KEY1'},
            ...         'value': {'S': 'value1'}
            ...     },
            ...     {
            ...         'setting_id': {'S': 'config2'},
            ...         'variable': {'S': 'KEY2'},
            ...         'value': {'N': '42'}
            ...     }
            ... ]
            >>> results = ConfigModel.boto3_items_to_dict_list(boto3_items)
            >>> # Returns:
            >>> # [
            >>> #     {'setting_id': 'config1', 'variable': 'KEY1', 'value': 'value1'},
            >>> #     {'setting_id': 'config2', 'variable': 'KEY2', 'value': 42}
            >>> # ]

            >>> # Empty list handling
            >>> results = ConfigModel.boto3_items_to_dict_list([])
            >>> # Returns: []

            >>> # None input handling (returns empty list)
            >>> results = ConfigModel.boto3_items_to_dict_list(None)
            >>> # Returns: []
        """
        if boto3_items is None:
            return []

        if not isinstance(boto3_items, list):
            raise TypeError(
                f"boto3_items must be a list, got {type(boto3_items).__name__}"
            )

        result: List[Dict[str, Any]] = []

        for index, item in enumerate(boto3_items):
            if not isinstance(item, dict):
                raise TypeError(
                    f"Item at index {index} must be a dictionary, "
                    f"got {type(item).__name__}"
                )
            result.append(cls.boto3_item_to_dict(item))

        return result

    @classmethod
    def generate_graphql_type(
        cls,
        type_name: Optional[str] = None,
        remove_auto_type_suffix: Optional[str] = "Model",
    ) -> GraphQLObjectType:
        if not type_name:
            type_name = cls.__name__

            if isinstance(remove_auto_type_suffix, str):
                remove_auto_type_suffix = str(remove_auto_type_suffix).strip()

                if remove_auto_type_suffix != type_name:
                    type_name = type_name.removesuffix(remove_auto_type_suffix)

        type_name = Utility.to_camel_case(type_name)

        return GraphQLTypeGenerator().generate_type_from_model(cls, type_name)
