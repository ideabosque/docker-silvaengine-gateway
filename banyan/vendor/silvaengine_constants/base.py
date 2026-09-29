#!/usr/bin/python
# -*- coding: utf-8 -*-
from enum import Enum
from typing import Any, Optional


class BaseEnum(Enum):
    @classmethod
    def has_member(cls, member: str) -> bool:
        try:
            getattr(cls, str(member).strip().upper())
            return True
        except AttributeError:
            return False

    @classmethod
    def get(cls, member: str, default: Optional[Any] = None) -> Any:
        try:
            return getattr(cls, str(member).strip().upper())
        except AttributeError:
            if isinstance(default, cls):
                return default

        return None
