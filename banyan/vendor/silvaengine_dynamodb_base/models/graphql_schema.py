#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

import os
from typing import Any, Callable, Dict

import pendulum
from pynamodb.attributes import UnicodeAttribute, UTCDateTimeAttribute
from pynamodb.indexes import AllProjection, GlobalSecondaryIndex
from pynamodb.pagination import ResultIterator

from silvaengine_constants import SwitchStatus
from silvaengine_utility.cache import hybrid_cache

from ..model import BaseModel

# se-* 配置表 TTL 缓存（与 connection.py 保持一致的开关）
_CACHE_TTL = int(os.environ.get("SILVAENGINE_CACHE_TTL", "300"))
_CACHE_ENABLED = lambda: str(os.environ.get("SILVAENGINE_CACHE_ENABLED", "true")).lower() not in (  # noqa: E731
    "false",
    "0",
    "no",
)


class GraphqlSchemaModel(BaseModel):
    class Meta(BaseModel.Meta):
        table_name = "se-graphql-schemas"

    endpoint_id = UnicodeAttribute(hash_key=True)
    operation = UnicodeAttribute(range_key=True)
    schema = UnicodeAttribute()
    module_name = UnicodeAttribute()
    custom_schema = UnicodeAttribute(null=True)
    created_at = UTCDateTimeAttribute()
    updated_at = UTCDateTimeAttribute()

    @classmethod
    def _build_range_key(
        cls,
        operation_type: str,
        operation_name: str,
        module_name: str,
    ) -> str:
        if not all([operation_type, operation_name, module_name]):
            raise ValueError("Invalid `operation_type` or `operation_name`")

        return f"{operation_type.strip().lower()}#{module_name.strip()}#{operation_name.strip()}"

    @classmethod
    def get_schema_picker(cls, endpoint_id: str) -> Callable:
        def _schema_picker(
            operation_type: str,
            operation_name: str,
            module_name: str,
            enable_preferred_custom_schema: bool = False,
        ) -> str:
            return cls.get_schema(
                endpoint_id=endpoint_id,
                operation_type=operation_type,
                operation_name=operation_name,
                module_name=module_name,
                enable_preferred_custom_schema=enable_preferred_custom_schema,
            )

        return _schema_picker

    @classmethod
    @hybrid_cache(ttl=_CACHE_TTL, cache_name="se_config", cache_enabled=_CACHE_ENABLED)
    def get_schema(
        cls,
        endpoint_id: str,
        operation_type: str,
        operation_name: str,
        module_name: str,
        enable_preferred_custom_schema: bool = False,
    ) -> str:
        item = cls.fetch(
            endpoint_id=endpoint_id,
            operation_type=operation_type,
            operation_name=operation_name,
            module_name=module_name,
        )

        if enable_preferred_custom_schema and item.custom_schema:
            schema = str(item.custom_schema).strip()

            if schema:
                return schema
        return str(item.schema).strip() if item.schema else ""

    @classmethod
    def store(
        cls,
        endpoint_id: str,
        operation_type: str,
        operation_name: str,
        schema: str,
        module_name: str,
    ) -> Dict[str, Any]:
        """Save a graphql schema model to DynamoDB."""
        try:
            if not all(
                [
                    endpoint_id,
                    operation_type,
                    operation_name,
                    schema,
                    module_name,
                ]
            ):
                raise ValueError("Invalid arguments")

            now = pendulum.now("UTC")

            return GraphqlSchemaModel(
                endpoint_id.strip().lower(),
                cls._build_range_key(
                    operation_type=operation_type,
                    operation_name=operation_name,
                    module_name=module_name,
                ),
                **{
                    "schema": schema.strip(),
                    "module_name": module_name.strip(),
                    "updated_at": now,
                    "created_at": now,
                },
            ).save()
        except Exception as e:
            raise ValueError(f"Failed to save graphql schema: {str(e)}")

    @classmethod
    def fetch(
        cls,
        endpoint_id: str,
        operation_type: str,
        operation_name: str,
        module_name: str,
    ) -> "GraphqlSchemaModel":
        """Get a graphql schema model from DynamoDB."""
        if not all([endpoint_id, operation_type, operation_name, module_name]):
            raise ValueError(
                "Invalid `endpoint_id`, `operation_type` or `operation_name`"
            )

        try:
            return GraphqlSchemaModel.get(
                hash_key=endpoint_id,
                range_key=cls._build_range_key(
                    operation_type=operation_type,
                    operation_name=operation_name,
                    module_name=module_name,
                ),
            )
        except Exception as e:
            raise ValueError(f"Failed to get graphql schema: {str(e)}")
