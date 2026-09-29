#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

from enum import Enum, unique


@unique
class AgentType(Enum):
    TRIAGE = "triage"
    TASK = "task"


@unique
class LLMUserRole(Enum):
    DEVELOPER = "developer"
    ASSISTANT = "assistant"
    TOOL = "tool"
