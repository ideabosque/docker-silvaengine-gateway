#!/usr/bin/python
# -*- coding: utf-8 -*-
from enum import Enum, unique


@unique
class Deployment(Enum):
    DEPLOYMENT_IN_DIFFERENT_REGION = 1
    DEPLOYMENT_IN_SAME_REGION = 2


@unique
class ExecuteMode(Enum):
    LOCAL = "local_for_all"
    SQS = "local_for_sqs"
    AWS_LAMBDA = "local_for_aws_lambda"


@unique
class AuthorizationType(Enum):
    REQUEST = 1
    TOKEN = 2


@unique
class RequestMethod(Enum):
    GET = 1
    POST = 2
    PUT = 3
    PATCH = 4
    DELETE = 5
    HEAD = 6


@unique
class AuthorizationAction(Enum):
    AUTHORIZE = 1
    VERIFY_PERMISSION = 2


@unique
class NamingConvention(Enum):
    CAMEL = 0
    SNAKE = 1
