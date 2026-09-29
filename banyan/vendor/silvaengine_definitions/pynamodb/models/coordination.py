#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"


from pynamodb.attributes import (
    ListAttribute,
    MapAttribute,
    UnicodeAttribute,
    UTCDateTimeAttribute,
)
from silvaengine_dynamodb_base import (
    BaseModel
)

class CoordinationModel(BaseModel):
    class Meta(BaseModel.Meta):
        table_name = "ace-coordinations"

    partition_key = UnicodeAttribute(hash_key=True)
    coordination_uuid = UnicodeAttribute(range_key=True)
    endpoint_id = UnicodeAttribute()
    part_id = UnicodeAttribute()
    coordination_name = UnicodeAttribute()
    coordination_description = UnicodeAttribute()
    agents = ListAttribute(of=MapAttribute)
    theme_uuid = UnicodeAttribute()
    updated_by = UnicodeAttribute()
    created_at = UTCDateTimeAttribute()
    updated_at = UTCDateTimeAttribute()
