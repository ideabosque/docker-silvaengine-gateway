#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

import logging
from typing import Any, Tuple

from graphene.types import ResolveInfo

from ...loader import SafeDataLoader, UnionKey
from ..models.coordination import CoordinationModel


class CoordinationLoader(SafeDataLoader):
    """
    Batch loader for CoordinationModel keyed by (partition_key, coordination_uuid).
    """

    def __init__(
        self,
        info: ResolveInfo,
        logger: logging.Logger = None,
        cache_ttl: int = 0,
        **kwargs,
    ):
        super().__init__(logger, cache_ttl, **kwargs)
        self._model = CoordinationModel
