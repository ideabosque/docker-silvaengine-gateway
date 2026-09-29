#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

from enum import Enum, unique


@unique
class SwitchStatus(Enum):
    INACTIVE = 0
    ACTIVE = 1
