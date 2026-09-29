#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "jeffreyw"

import functools
import logging
import traceback
import secrets

from typing import Any, Dict

import pendulum
from graphene import ResolveInfo
from pynamodb.attributes import (
    MapAttribute,
    UnicodeAttribute,
    UTCDateTimeAttribute,
    ListAttribute,
    NumberAttribute,
    BooleanAttribute
)
from pynamodb.indexes import AllProjection, LocalSecondaryIndex
from tenacity import retry, stop_after_attempt, wait_exponential

from silvaengine_dynamodb_base import (
    BaseModel,
    delete_decorator,
    insert_update_decorator,
    monitor_decorator,
    resolve_list_decorator,
)
from silvaengine_utility import method_cache
from silvaengine_utility.serializer import Serializer


class UsageLimitModel(BaseModel):
    class Meta(BaseModel.Meta):
        table_name = "aace-usage_limit"

    partition_key = UnicodeAttribute(hash_key=True)
    usage_key = UnicodeAttribute(range_key=True)
    
    usage_limit = NumberAttribute()
    allow_overage = BooleanAttribute()
    period_start = UTCDateTimeAttribute()
    period_end = UTCDateTimeAttribute()

    created_from = UnicodeAttribute()
    # status = UnicodeAttribute()
    
    created_at = UTCDateTimeAttribute()
    updated_at = UTCDateTimeAttribute()


def get_usage_limit(partition_key: str, usage_key: str) -> Any:
    try:
        return UsageLimitModel.get(partition_key, usage_key)
    except Exception as e:
        return None

def insert_update_usage_limit(**kwargs):
    partition_key = kwargs.get("partition_key")
    usage_key = kwargs.get("usage_key")
    entity = get_usage_limit(partition_key, usage_key)
    if entity is None:
        cols = {
            "usage_limit": kwargs.get("usage_limit"),
            "allow_overage": kwargs.get("allow_overage"),
            "period_start": kwargs.get("period_start"),
            "period_end": kwargs.get("period_end"),
            "created_from": kwargs.get("created_from"),
            # "status": kwargs.get("status"),
            "created_at": pendulum.now("UTC"),
            "updated_at": pendulum.now("UTC"),
        }

        UsageLimitModel(
            partition_key,
            usage_key,
            **cols,
        ).save()
        return

    actions = [
        UsageLimitModel.updated_at.set(pendulum.now("UTC")),
    ]
    field_map = {
        "usage_limit": UsageLimitModel.usage_limit,
        "allow_overage": UsageLimitModel.allow_overage,
        "period_start": UsageLimitModel.period_start,
        "period_end": UsageLimitModel.period_end,
        "created_from": UsageLimitModel.created_from,
        # "status": UsageLimitModel.status,
    }

    for key, field in field_map.items():
        if key in kwargs:
            actions.append(field.set(kwargs[key]))

    entity.update(actions=actions)
    return

def delete_usage_limit(partition_key: str, usage_key: str):
    entity = get_usage_limit(partition_key, usage_key)
    if entity is not None:
        entity.delete()
    return True