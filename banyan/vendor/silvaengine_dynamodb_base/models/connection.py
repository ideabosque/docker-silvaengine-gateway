#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

import os

from pynamodb.attributes import ListAttribute, MapAttribute, UnicodeAttribute

from silvaengine_utility.cache import hybrid_cache

from ..model import BaseModel

# se-* 配置表 TTL 缓存（低频变更的配置/路由数据；SILVAENGINE_CACHE_TTL=0 可禁用，
# SILVAENGINE_CACHE_ENABLED=false 可紧急回退直查）
_CACHE_TTL = int(os.environ.get("SILVAENGINE_CACHE_TTL", "300"))
_CACHE_ENABLED = lambda: str(os.environ.get("SILVAENGINE_CACHE_ENABLED", "true")).lower() not in (  # noqa: E731
    "false",
    "0",
    "no",
)


class FunctionMap(MapAttribute):
    aws_lambda_arn = UnicodeAttribute()
    function = UnicodeAttribute()
    setting = UnicodeAttribute()


class ConnectionModel(BaseModel):
    class Meta(BaseModel.Meta):
        table_name = "se-connections"

    endpoint_id = UnicodeAttribute(hash_key=True)
    api_key = UnicodeAttribute(range_key=True, default="#####")
    functions = ListAttribute(of=FunctionMap)
    whitelist = ListAttribute()

    @classmethod
    @hybrid_cache(ttl=_CACHE_TTL, cache_name="se_config", cache_enabled=_CACHE_ENABLED)
    def get(cls, *args, **kwargs):
        return super(ConnectionModel, cls).get(*args, **kwargs)
