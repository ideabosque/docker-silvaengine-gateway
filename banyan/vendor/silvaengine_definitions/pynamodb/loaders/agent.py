#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

import logging
from typing import Any, Tuple, Dict, List

from promise import Promise
from promise.dataloader import DataLoader

from graphene.types import ResolveInfo

from ...loader import SafeDataLoader, UnionKey
from ...utils import normalize_model
from ..models.agent import AgentModel


class AgentLoader(SafeDataLoader):
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
        self._model = AgentModel

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
                        results = self.model.agent_uuid_index.query(
                            partition_key,
                            self.model.agent_uuid == model_uuid,
                            filter_condition=(self.model.status == "active"),
                            scan_index_forward=False,
                            limit=1,
                        )
                        task = results.next()
                        normalized = normalize_model(task)
                        key_map[(partition_key, model_uuid)] = normalized

                        # Cache the result if enabled
                        if self.cache_enabled:
                            cache_key = f"{partition_key}:{model_uuid}"
                            self.cache.set(cache_key, normalized, ttl=self.catch_ttl)
                        # return agent
                    except StopIteration:
                        pass
                        # task = self.model.get(partition_key, model_uuid)
                        
                    except self.model.DoesNotExist:
                        pass

            except Exception as exc:
                if self.logger:
                    self.logger.exception(exc)

        return Promise.resolve([key_map.get(key) for key in keys])
