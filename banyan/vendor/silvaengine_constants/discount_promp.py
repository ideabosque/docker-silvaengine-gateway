#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function


# Status constants for DiscountPrompt
class DiscountPromptStatus:
    IN_REVIEW = "in_review"
    ACTIVE = "active"
    INACTIVE = "inactive"


# Scope constants for DiscountPrompt
class DiscountPromptScope:
    GLOBAL = "global"
    SEGMENT = "segment"
    ITEM = "item"
    PROVIDER_ITEM = "provider_item"
