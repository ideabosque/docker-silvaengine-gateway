#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

from enum import Enum, unique

from .base import BaseEnum


@unique
class EventType(BaseEnum):
    UNKNOWN = 0
    HTTP_API = 1
    REST_API = 2
    WEBSOCKET = 3
    S3 = 4
    SQS = 5
    SNS = 6
    DYNAMODB = 7
    COGNITO = 8
    EVENT_BRIDGE = 9
    CLOUDWATCH_LOG = 10
    LAMBDA_INVOCATION = 11


@unique
class InvocationType(Enum):
    EVENT = "Event"
    REQUEST_RESPONSE = "RequestResponse"
    DRY_RUN = "DryRun"
