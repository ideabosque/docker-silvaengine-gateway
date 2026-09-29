#!/usr/bin/python
# -*- coding: utf-8 -*-
from enum import Enum, unique
from typing import Optional


@unique
class OperationType(Enum):
    QUERY = "Query"
    MUTATION = "Mutation"
    SUBSCRIPTION = "Subscription"

    @classmethod
    def has_member(cls, member: str) -> bool:
        try:
            getattr(cls, str(member).strip().upper())
            return True
        except AttributeError:
            return False

    @classmethod
    def get(
        cls, member: str, default: Optional["OperationType"] = None
    ) -> "OperationType":
        try:
            return getattr(cls, str(member).strip().upper())
        except AttributeError:
            if isinstance(default, OperationType):
                return default

            return OperationType.QUERY
