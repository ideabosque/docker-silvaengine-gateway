#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

from pynamodb.attributes import BooleanAttribute, UnicodeAttribute

from ..model import BaseModel


class HookModel(BaseModel):
    class Meta(BaseModel.Meta):
        table_name = "se-hooks"

    api_id = UnicodeAttribute(hash_key=True)
    module_name = UnicodeAttribute(range_key=True)
    function_name = UnicodeAttribute()
    is_async = BooleanAttribute(default=False)
    is_interruptible = BooleanAttribute(default=False)
    status = BooleanAttribute(default=True)
    description = UnicodeAttribute()
