#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

from graphene.types import ResolveInfo

__author__ = "bibow"

import logging
from typing import Any, Dict, List, Tuple

from promise import Promise
from promise.dataloader import DataLoader
from silvaengine_utility.cache import HybridCacheEngine

from .utils import normalize_model

UnionKey = Tuple[str, str]  # (partition_key, model_uuid)


class SafeDataLoader(DataLoader):
    """
    Base DataLoader that swallows and logs errors rather than breaking the entire
    request. This keeps individual load failures isolated.

    All batch loaders should inherit from this class to ensure consistent
    error handling and caching behavior.
    """

    def __init__(
        self,
        info: ResolveInfo = None,
        logger: logging.Logger = None,
        cache_ttl: int = 0,
        **kwargs,
    ):
        """
        Initialize SafeDataLoader.

        Args:
            logger: Logger instance for error logging
            cache_enabled: Whether to enable caching for this loader
            **kwargs: Additional arguments passed to DataLoader
        """
        super(SafeDataLoader, self).__init__(**kwargs)
        self.logger = logger
        self.catch_ttl = cache_ttl
        self.cache_enabled = True if cache_ttl > 0 else False
        self._model = None

    @property
    def cache(self):
        if not self.model:
            raise ValueError("Necessary to bind a data model to the current loader")

        if self.cache_enabled:
            return HybridCacheEngine(f"model:{str(self.model)}")

        return None

    @property
    def model(self):
        if not self._model:
            raise NotImplementedError("Necessary to bind a data model to `_model`")

        return self._model

    def dispatch(self):
        """
        Dispatch the batch load, catching and logging any errors.

        Returns:
            Result of batch load operation

        Raises:
            Exception: Re-raises caught exceptions after logging
        """
        try:
            return super(SafeDataLoader, self).dispatch()
        except Exception as exc:
            if self.logger:
                self.logger.exception(exc)
            raise

    def batch_load_fn(self, keys: List[UnionKey]) -> Promise:
        """
        Batch load tasks by their composite keys.

        Args:
            keys: List of (partition_key, model_uuid) tuples

        Returns:
            Promise resolving to list of task dicts in same order as keys

        IMPORTANT: If the batch loading method or logic of the corresponding model is different from the current default loading method, please override this method in the corresponding subclass.
        """
        if not self.model:
            raise ValueError(
                "It is necessary to bind a data model to the current loader"
            )

        unique_keys = list(dict.fromkeys(keys))
        key_map: Dict[UnionKey, Dict[str, Any]] = {}
        uncached_keys = []

        # Check cache first if enabled
        if self.cache_enabled:
            for key in unique_keys:
                cache_key = f"{key[0]}:{key[1]}"  # partition_key:model_uuid
                cached_item = self.cache.get(cache_key)

                if cached_item:
                    key_map[key] = cached_item
                else:
                    uncached_keys.append(key)
        else:
            uncached_keys = unique_keys

        # Fetch uncached items from database
        if uncached_keys:
            try:
                for partition_key, model_uuid in uncached_keys:
                    try:
                        task = self.model.get(partition_key, model_uuid)
                        normalized = normalize_model(task)
                        key_map[(partition_key, model_uuid)] = normalized

                        # Cache the result if enabled
                        if self.cache_enabled:
                            cache_key = f"{partition_key}:{model_uuid}"
                            self.cache.set(cache_key, normalized, ttl=self.catch_ttl)
                    except self.model.DoesNotExist:
                        pass

            except Exception as exc:
                if self.logger:
                    self.logger.exception(exc)

        return Promise.resolve([key_map.get(key) for key in keys])
