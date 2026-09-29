#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

import logging
from typing import Any, Tuple

from graphene.types import ResolveInfo

from ...loader import SafeDataLoader, UnionKey
from ..models.theme_setting import ThemeSettingModel


class ThemeSettingLoader(SafeDataLoader):
    """
    Batch loader for ThemeSettingModel keyed by (partition_key, theme_uuid).
    """

    def __init__(
        self,
        info: ResolveInfo,
        logger: logging.Logger = None,
        cache_ttl: int = 0,
        **kwargs,
    ):
        super().__init__(logger, cache_ttl, **kwargs)
        self._model = ThemeSettingModel
