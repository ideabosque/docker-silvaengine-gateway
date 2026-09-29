# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

from graphene import ObjectType, Int


class ListObjectType(ObjectType):
    page_size = Int()
    page_number = Int()
    total = Int()
