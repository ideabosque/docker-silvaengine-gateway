#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

import os

from pynamodb.attributes import (
    BooleanAttribute,
    ListAttribute,
    MapAttribute,
    UnicodeAttribute,
)

from silvaengine_utility.cache import hybrid_cache

from ..model import BaseModel

# se-* 配置表 TTL 缓存（与 connection.py 保持一致的开关）
_CACHE_TTL = int(os.environ.get("SILVAENGINE_CACHE_TTL", "300"))
_CACHE_ENABLED = lambda: str(os.environ.get("SILVAENGINE_CACHE_ENABLED", "true")).lower() not in (  # noqa: E731
    "false",
    "0",
    "no",
)


class OperationMap(MapAttribute):
    query = ListAttribute()
    mutation = ListAttribute()


class ConfigMap(MapAttribute):
    class_name = UnicodeAttribute()
    funct_type = UnicodeAttribute()
    methods = ListAttribute()
    module_name = UnicodeAttribute()
    setting = UnicodeAttribute()
    auth_required = BooleanAttribute(default=False)
    graphql = BooleanAttribute(default=False)
    operations = OperationMap()


class FunctionModel(BaseModel):
    class Meta(BaseModel.Meta):
        table_name = "se-functions"

    aws_lambda_arn = UnicodeAttribute(hash_key=True)
    function = UnicodeAttribute(range_key=True)
    area = UnicodeAttribute()
    config = ConfigMap()

    @classmethod
    @hybrid_cache(ttl=_CACHE_TTL, cache_name="se_config", cache_enabled=_CACHE_ENABLED)
    def get(cls, *args, **kwargs):
        return super(FunctionModel, cls).get(*args, **kwargs)
