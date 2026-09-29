#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

from pynamodb.attributes import BooleanAttribute, UnicodeAttribute

from ..model import BaseModel


class EndpointModel(BaseModel):
    class Meta(BaseModel.Meta):
        table_name = "se-endpoints"

    endpoint_id = UnicodeAttribute(hash_key=True)
    special_connection = BooleanAttribute(default=False)
