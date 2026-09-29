#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

import os
from typing import Any, Dict, List

from pynamodb.attributes import UnicodeAttribute

from silvaengine_utility.cache import hybrid_cache

from ..model import AnyAttribute, BaseModel

# se-* 配置表 TTL 缓存（与 connection.py 保持一致的开关）
_CACHE_TTL = int(os.environ.get("SILVAENGINE_CACHE_TTL", "300"))
_CACHE_ENABLED = lambda: str(os.environ.get("SILVAENGINE_CACHE_ENABLED", "true")).lower() not in (  # noqa: E731
    "false",
    "0",
    "no",
)


class ConfigModel(BaseModel):
    class Meta(BaseModel.Meta):
        abstract = True
        table_name = "se-configdata"

    setting_id = UnicodeAttribute(hash_key=True)
    variable = UnicodeAttribute()
    value = AnyAttribute()

    @classmethod
    @hybrid_cache(ttl=_CACHE_TTL, cache_name="se_config", cache_enabled=_CACHE_ENABLED)
    def find(
        cls,
        setting_id: str,
        return_dict: bool = True,
    ) -> Dict[str, Any] | List[BaseModel]:
        """
        Fetch a setting from DynamoDB based on the setting ID with caching.
        :param setting_id: The ID of the setting.
        :return: A dictionary of settings.
        """
        setting_id = str(setting_id).strip()

        if not setting_id:
            return {} if return_dict else []

        try:
            result = cls.query_raw(hash_key=setting_id)

            if result.get("Count", 0) < 1:
                raise ValueError(
                    f"Cannot find values with the setting_id ({setting_id})."
                )

            items = result.get("Items", [])

            if len(items) < 1:
                return []
            elif return_dict:
                result = {}
                settings = cls.boto3_items_to_dict_list(items)

                if not settings or not isinstance(settings, list):
                    return result

                for item in settings:
                    if type(item) is dict and "variable" in item and "value" in item:
                        result.update({item.get("variable"): item.get("value")})

                return result

            return cls.boto3_items_to_models(items)
        except Exception as e:
            if isinstance(e, ValueError):
                raise e
            raise ValueError(f"Failed to get setting {setting_id}: {str(e)}")
