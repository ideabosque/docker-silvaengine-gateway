# -*- coding: utf-8 -*-
from __future__ import annotations

__author__ = "bibow"

import logging
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Callable, Dict, List, Optional


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value != ""
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


@dataclass
class CacheEntityMeta:
    """Static metadata used for cache purging per entity type."""

    entity_type: str
    module: str
    model_class_name: Optional[str]
    getter_name: str
    list_resolver_path: Optional[str]
    cache_keys: List[str]
    module_ref: Optional[Any] = None
    getter: Optional[Any] = None
    model_class: Optional[Any] = None
    list_resolver: Optional[Callable[..., Any]] = None


@dataclass(frozen=True)
class CacheConfigResolvers:
    """Callable helpers that provide cache configuration metadata."""

    get_cache_entity_config: Optional[Callable[[], Dict[str, Dict[str, Any]]]] = None
    get_cache_relationships: Optional[Callable[[], Dict[str, List[Dict[str, Any]]]]] = (
        None
    )
    queries_module_base: Optional[str] = None


class CascadingCachePurger:
    """Reusable cascading cache purger that relies on configuration callbacks."""

    def __init__(self, config: CacheConfigResolvers):
        self._config = config
        self._entity_registry: Dict[str, CacheEntityMeta] = {}

    def purge_entity_cascading_cache(
        self,
        logger: logging.Logger,
        entity_type: str,
        *,
        context_keys: Optional[Dict[str, Any]] = None,
        entity_keys: Optional[Dict[str, Any]] = None,
        cascade_depth: int = 3,
        custom_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Purge entity cache and cascade to related entities.

        Args:
            logger: Logger instance
            entity_type: Type of entity to purge
            context_keys: Context information (e.g., {"tenant_id": "abc123"})
            entity_keys: Entity-specific keys for cache resolution
            cascade_depth: Maximum depth for cascading purges
            custom_options: Custom options for cache resolution {"custom_getter": "", "custom_list_resolver": "", "custom_cache_keys": []}
        """
        # Merge context_keys into entity_keys for unified resolution
        merged_keys = {}
        if context_keys:
            merged_keys.update(context_keys)
        if entity_keys:
            merged_keys.update(entity_keys)

        purge_results = {
            "entity_type": entity_type,
            "entity_keys": merged_keys,
            "context_keys": context_keys,
            "individual_cache_cleared": False,
            "list_cache_cleared": False,
            "cascaded_levels": [],
            "total_child_caches_cleared": 0,
            "total_individual_children_cleared": 0,
            "errors": [],
        }

        try:
            if merged_keys:
                try:
                    individual_result = self._clear_individual_entity_cache(
                        logger,
                        entity_type,
                        merged_keys,
                        custom_options,
                    )
                    purge_results["individual_cache_cleared"] = individual_result
                except Exception as exc:
                    purge_results["errors"].append(
                        f"Error clearing individual {entity_type} cache: {str(exc)}"
                    )

            if custom_options is not None:
                return purge_results
            
            try:
                list_result = self._clear_entity_list_cache(logger, entity_type)
                purge_results["list_cache_cleared"] = list_result
            except Exception as exc:
                purge_results["errors"].append(
                    f"Error clearing {entity_type} list cache: {str(exc)}"
                )

            cascade_result = self._cascade_purge_child_caches(
                logger,
                parent_entity_type=entity_type,
                context_keys=context_keys,
                entity_keys=merged_keys,
                cascade_depth=cascade_depth,
            )
            purge_results["cascaded_levels"] = cascade_result["cascaded_levels"]
            purge_results["total_child_caches_cleared"] = cascade_result[
                "total_caches_cleared"
            ]
            purge_results["total_individual_children_cleared"] = cascade_result[
                "total_individual_children_cleared"
            ]
            purge_results["errors"].extend(cascade_result["errors"])
        except Exception as exc:
            purge_results["errors"].append(
                f"Error in purge_entity_cascading_cache: {str(exc)}"
            )
            logger.error(
                "Error in purge_entity_cascading_cache: %s",
                str(exc),
            )

        return purge_results

    def _clear_individual_entity_cache(
        self,
        logger: logging.Logger,
        entity_type: str,
        entity_keys: Dict[str, Any],
        custom_options: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Clear individual entity cache using only entity_keys."""
        try:
            meta = self._get_entity_meta(entity_type, custom_options)
            if (
                meta is None
                or meta.getter is None
                or not hasattr(meta.getter, "cache_delete")
            ):
                logger.debug(
                    "Cache delete not configured for entity_type=%s",
                    entity_type,
                )
                return False

            cache_args = self._resolve_cache_args(
                meta,
                entity_keys=entity_keys,
            )
            if not cache_args:
                logger.debug(
                    "Unable to resolve cache keys for entity_type=%s using keys=%s",
                    entity_type,
                    entity_keys,
                )
                return False

            meta.getter.cache_delete(*cache_args)
            logger.info("Cleared individual %s cache", entity_type)
            return True

        except Exception as exc:
            logger.error(
                "Error clearing individual %s cache: %s",
                entity_type,
                str(exc),
            )
        return False

    def _clear_entity_list_cache(
        self,
        logger: logging.Logger,
        entity_type: str,
    ) -> bool:
        try:
            meta = self._get_entity_meta(entity_type)
            list_resolver = meta.list_resolver if meta else None

            if list_resolver is None:
                list_resolver_name = f"resolve_{entity_type}_list"
                module_path = self._build_queries_module_path(entity_type)
                if module_path:
                    try:
                        resolver_module = import_module(module_path)
                        list_resolver = getattr(
                            resolver_module, list_resolver_name, None
                        )
                    except ImportError:
                        list_resolver = None

            if list_resolver and hasattr(list_resolver, "cache_clear"):
                list_resolver.cache_clear()
                logger.info("Cleared %s list cache", entity_type)
                return True

        except Exception as exc:
            logger.error("Error clearing %s list cache: %s", entity_type, str(exc))

        return False

    def _cascade_purge_child_caches(
        self,
        logger: logging.Logger,
        parent_entity_type: str,
        context_keys: Optional[Dict[str, Any]] = None,
        entity_keys: Optional[Dict[str, Any]] = None,
        cascade_depth: int = 3,
    ) -> Dict[str, Any]:
        cascade_results = {
            "cascaded_levels": [],
            "total_caches_cleared": 0,
            "total_individual_children_cleared": 0,
            "errors": [],
        }

        try:
            if not self._config.get_cache_relationships:
                return cascade_results

            relationships = self._config.get_cache_relationships()

            max_depth = (
                float("inf")
                if cascade_depth is None or cascade_depth < 0
                else max(0, cascade_depth)
            )

            entities_to_process = [(parent_entity_type, 0)]
            processed_entities = set()

            while entities_to_process:
                current_entity, current_level = entities_to_process.pop(0)

                if current_level > max_depth:
                    continue

                if current_entity in processed_entities:
                    continue

                processed_entities.add(current_entity)
                children = relationships.get(current_entity, [])

                if not children:
                    continue

                level_results = {
                    "level": current_level,
                    "parent_entity": current_entity,
                    "child_caches_cleared": [],
                    "individual_children_cleared": [],
                }

                for child in children:
                    module_name = child.get("module")
                    resolver_name = child.get("list_resolver")
                    child_entity_type = child.get("entity_type")
                    dependency_key = child.get("dependency_key")

                    try:
                        if (
                            not module_name
                            or not resolver_name
                            or not child_entity_type
                        ):
                            raise ValueError(
                                f"Incomplete cache relationship for parent {current_entity}: {child}"
                            )

                        module_path = self._build_queries_module_path(module_name)
                        resolver_module = import_module(module_path)
                        resolver_func = getattr(resolver_module, resolver_name)

                        if hasattr(resolver_func, "cache_clear"):
                            resolver_func.cache_clear()
                            level_results["child_caches_cleared"].append(
                                {
                                    "entity_type": child_entity_type,
                                    "resolver": resolver_name,
                                    "dependency": dependency_key,
                                    "module": module_name,
                                }
                            )
                            cascade_results["total_caches_cleared"] += 1

                            logger.info(
                                "L%s: Cleared %s list cache (child of %s)",
                                current_level,
                                child_entity_type,
                                current_entity,
                            )

                        if (
                            current_level == 0
                            and entity_keys
                            and context_keys is not None
                        ):
                            individual_count = self._clear_individual_child_entities(
                                logger,
                                current_entity,
                                child,
                                context_keys,
                                entity_keys,
                            )
                            if individual_count > 0:
                                level_results["individual_children_cleared"].append(
                                    {
                                        "entity_type": child_entity_type,
                                        "count": individual_count,
                                        "dependency": dependency_key,
                                    }
                                )
                                cascade_results[
                                    "total_individual_children_cleared"
                                ] += individual_count

                        next_level = current_level + 1
                        if next_level <= max_depth and child_entity_type:
                            entities_to_process.append((child_entity_type, next_level))

                    except ImportError as exc:
                        cascade_results["errors"].append(
                            f"Could not import {module_name}.{resolver_name}: {str(exc)}"
                        )
                    except AttributeError as exc:
                        cascade_results["errors"].append(
                            f"Resolver function not found: {str(exc)}"
                        )
                    except Exception as exc:
                        cascade_results["errors"].append(
                            f"Error clearing {child_entity_type} cache: {str(exc)}"
                        )

                if (
                    level_results["child_caches_cleared"]
                    or level_results["individual_children_cleared"]
                ):
                    cascade_results["cascaded_levels"].append(level_results)

        except Exception as exc:
            cascade_results["errors"].append(
                f"Error in _cascade_purge_child_caches_universal: {str(exc)}"
            )
            logger.error(
                "Error in _cascade_purge_child_caches_universal: %s",
                str(exc),
            )

        return cascade_results

    def _clear_individual_child_entities(
        self,
        logger: logging.Logger,
        parent_entity_type: str,
        child_config: Dict[str, Any],
        context_keys: Optional[Dict[str, Any]],
        parent_entity_keys: Optional[Dict[str, Any]],
    ) -> int:
        cleared_count = 0
        child_entity_type = child_config.get("entity_type")
        if not child_entity_type:
            return 0

        dependency_key = child_config.get("dependency_key")
        parent_key_name = child_config.get("parent_key", dependency_key)

        if not parent_key_name:
            return 0

        parent_key_value = (parent_entity_keys or {}).get(parent_key_name)

        if not _has_value(parent_key_value):
            logger.debug(
                "No parent key value found for %s in %s entity keys: %s",
                parent_key_name,
                parent_entity_type,
                parent_entity_keys,
            )
            return 0

        meta = self._get_entity_meta(child_entity_type)
        if (
            meta is None
            or meta.getter is None
            or not hasattr(meta.getter, "cache_delete")
        ):
            logger.debug(
                "Cache metadata missing for child entity %s (parent %s)",
                child_entity_type,
                parent_entity_type,
            )
            return 0

        child_model_class = meta.model_class
        if child_model_class is None and meta.model_class_name:
            try:
                child_model_class = getattr(meta.module_ref, meta.model_class_name)
                meta.model_class = child_model_class
            except AttributeError:
                child_model_class = None

        if child_model_class is None:
            logger.warning(
                "Could not load model class for %s when clearing %s children",
                child_entity_type,
                parent_entity_type,
            )
            return 0

        if isinstance(parent_key_value, (list, tuple, set)):
            parent_values = [value for value in parent_key_value if _has_value(value)]
        else:
            parent_values = [parent_key_value]

        direct_clear = child_config.get("direct_clear_parent_ids", False)
        seen_values = set()

        for raw_value in parent_values:
            normalized_value = (
                raw_value.strip() if isinstance(raw_value, str) else raw_value
            )
            if not _has_value(normalized_value):
                continue

            try:
                if normalized_value in seen_values:
                    continue
                seen_values.add(normalized_value)
            except TypeError:
                pass

            if direct_clear:
                cleared_for_value = self._direct_clear_child_cache(
                    logger,
                    meta,
                    context_keys,
                    normalized_value,
                )
            else:
                cleared_for_value = self._query_and_clear_child_entities(
                    logger,
                    meta=meta,
                    child_model_class=child_model_class,
                    dependency_key=dependency_key,
                    parent_key_value=normalized_value,
                    context_keys=context_keys,
                    parent_entity_type=parent_entity_type,
                )

            cleared_count += cleared_for_value

        return cleared_count

    def _query_and_clear_child_entities(
        self,
        logger: logging.Logger,
        meta: CacheEntityMeta,
        child_model_class: Any,
        dependency_key: Optional[str],
        parent_key_value: Any,
        context_keys: Optional[Dict[str, Any]],
        parent_entity_type: str = "unknown",
    ) -> int:
        if meta.getter is None or not hasattr(meta.getter, "cache_delete"):
            logger.debug(
                "Cache delete not available for child entity %s (parent %s)",
                meta.entity_type,
                parent_entity_type,
            )
            return 0

        cleared_count = 0

        try:
            if dependency_key:
                index_name = f"{dependency_key}_index"
                if hasattr(child_model_class, index_name):
                    try:
                        index = getattr(child_model_class, index_name)
                        entities = None

                        # Try context-aware query using the first context key found
                        context_value = None
                        context_attr = None
                        if context_keys:
                            for attr_name, attr_value in context_keys.items():
                                if hasattr(child_model_class, attr_name) and _has_value(
                                    attr_value
                                ):
                                    context_value = attr_value
                                    context_attr = attr_name
                                    break

                        if context_value is not None and context_attr is not None:
                            try:
                                entities = index.query(
                                    context_value,
                                    getattr(child_model_class, dependency_key)
                                    == parent_key_value,
                                )
                            except Exception as pattern_exc:
                                logger.debug(
                                    "Pattern 1 (%s + %s) failed for %s->%s: %s",
                                    context_attr,
                                    dependency_key,
                                    parent_entity_type,
                                    meta.entity_type,
                                    str(pattern_exc),
                                )

                        if entities is None:
                            try:
                                entities = index.query(parent_key_value)
                            except Exception as pattern_exc:
                                logger.debug(
                                    "Pattern 2 (direct %s) failed for %s->%s: %s",
                                    dependency_key,
                                    parent_entity_type,
                                    meta.entity_type,
                                    str(pattern_exc),
                                )

                        if entities is not None:
                            cleared_count = self._clear_entities_cache(
                                logger,
                                entities,
                                meta=meta,
                                context_keys=context_keys,
                            )

                            if logger and cleared_count > 0:
                                logger.info(
                                    "Cleared %s individual %s caches using %s for %s parent",
                                    cleared_count,
                                    meta.entity_type,
                                    index_name,
                                    parent_entity_type,
                                )

                            if cleared_count > 0:
                                return cleared_count

                    except Exception as exc:
                        logger.warning(
                            "Error using %s for %s->%s: %s",
                            index_name,
                            parent_entity_type,
                            meta.entity_type,
                            str(exc),
                        )

            if hasattr(child_model_class, "query"):
                try:
                    entities = (
                        child_model_class.query(parent_key_value)
                        if dependency_key
                        else child_model_class.query()
                    )
                    cleared_count = self._clear_entities_cache(
                        logger,
                        entities,
                        meta=meta,
                        context_keys=context_keys,
                    )

                    if logger and cleared_count > 0:
                        logger.info(
                            "Cleared %s individual %s caches using direct query for %s parent",
                            cleared_count,
                            meta.entity_type,
                            parent_entity_type,
                        )

                    if cleared_count > 0:
                        return cleared_count

                except Exception as exc:
                    logger.warning(
                        "Error using direct query for %s->%s: %s",
                        parent_entity_type,
                        meta.entity_type,
                        str(exc),
                    )

            attribute = (
                getattr(child_model_class, dependency_key, None)
                if dependency_key
                else None
            )
            if attribute is not None:
                try:
                    if hasattr(attribute, "contains"):
                        attr_condition = attribute.contains(parent_key_value)
                    else:
                        attr_condition = attribute == parent_key_value

                    # Add context condition if available
                    if context_keys:
                        for attr_name, attr_value in context_keys.items():
                            if hasattr(child_model_class, attr_name) and _has_value(
                                attr_value
                            ):
                                context_condition = (
                                    getattr(child_model_class, attr_name) == attr_value
                                )
                                attr_condition = context_condition & attr_condition
                                break

                    entities = child_model_class.scan(attr_condition)
                    cleared_count = self._clear_entities_cache(
                        logger,
                        entities,
                        meta=meta,
                        context_keys=context_keys,
                    )

                    if logger and cleared_count > 0:
                        logger.info(
                            "Cleared %s individual %s caches using scan for %s parent",
                            cleared_count,
                            meta.entity_type,
                            parent_entity_type,
                        )

                    if cleared_count > 0:
                        return cleared_count

                except Exception as exc:
                    logger.warning(
                        "Error using scan for %s->%s with dependency %s: %s",
                        parent_entity_type,
                        meta.entity_type,
                        dependency_key,
                        str(exc),
                    )

        except Exception as exc:
            logger.error(
                "Error in _query_and_clear_child_entities for %s->%s: %s",
                parent_entity_type,
                meta.entity_type,
                str(exc),
            )

        return cleared_count

    def _direct_clear_child_cache(
        self,
        logger: logging.Logger,
        meta: CacheEntityMeta,
        context_keys: Optional[Dict[str, Any]],
        child_identifier: Any,
    ) -> int:
        if not _has_value(child_identifier):
            return 0

        if meta.getter is None or not hasattr(meta.getter, "cache_delete"):
            logger.debug(
                "Direct clear skipped; cache_delete missing for %s",
                meta.entity_type,
            )
            return 0

        # Build entity_keys from context_keys and identifier
        entity_keys = {}
        if context_keys:
            entity_keys.update(context_keys)

        # Add the identifier to appropriate key based on cache_keys configuration
        for cache_key in meta.cache_keys:
            if cache_key.startswith("key:"):
                key_name = cache_key[4:]
                entity_keys[key_name] = child_identifier
                break

        cache_args = self._resolve_cache_args(
            meta,
            entity_keys=entity_keys,
            identifier=child_identifier,
        )
        if not cache_args:
            logger.debug(
                "Unable to resolve cache keys for direct clear of %s using identifier %s",
                meta.entity_type,
                child_identifier,
            )
            return 0

        try:
            meta.getter.cache_delete(*cache_args)
            logger.info(
                "Directly cleared %s cache for identifier %s",
                meta.entity_type,
                child_identifier,
            )
            return 1
        except Exception as exc:
            logger.debug(
                "Direct cache delete failed for %s: %s",
                meta.entity_type,
                str(exc),
            )
        return 0

    def _clear_entities_cache(
        self,
        logger: logging.Logger,
        entities: Any,
        meta: CacheEntityMeta,
        context_keys: Optional[Dict[str, Any]],
    ) -> int:
        if meta.getter is None or not hasattr(meta.getter, "cache_delete"):
            logger.debug(
                "Cache delete not available when clearing %s entities",
                meta.entity_type,
            )
            return 0

        if entities is None:
            logger.debug("No entities found for %s", meta.entity_type)
            return 0

        if not hasattr(entities, "__iter__") or isinstance(entities, (str, bytes)):
            entities = [entities]

        cleared_count = 0

        for entity in entities:
            # Build entity_keys from context_keys and entity attributes
            entity_keys = {}
            if context_keys:
                entity_keys.update(context_keys)

            # Extract entity attributes based on cache_keys configuration
            for cache_key in meta.cache_keys:
                if cache_key.startswith("key:"):
                    attr_name = cache_key[4:]
                    if hasattr(entity, attr_name):
                        entity_keys[attr_name] = getattr(entity, attr_name)
                elif cache_key.startswith("context:"):
                    context_attr = cache_key[8:]
                    if hasattr(entity, context_attr):
                        entity_keys[context_attr] = getattr(entity, context_attr)

            cache_args = self._resolve_cache_args(
                meta,
                entity_keys=entity_keys,
                entity=entity,
            )
            if not cache_args:
                entity_descriptor = getattr(entity, "id", None)
                if not _has_value(entity_descriptor) and meta.cache_keys:
                    token = meta.cache_keys[0]
                    if token.startswith("key:"):
                        entity_descriptor = getattr(entity, token[4:], None)
                    elif token.startswith("attr:"):
                        entity_descriptor = getattr(entity, token[5:], None)
                if not _has_value(entity_descriptor):
                    entity_descriptor = repr(entity)
                logger.debug(
                    "Unable to resolve cache args for %s entity %s",
                    meta.entity_type,
                    entity_descriptor,
                )
                continue

            try:
                meta.getter.cache_delete(*cache_args)
                cleared_count += 1
            except Exception as exc:
                logger.warning(
                    "Error clearing cache for %s entity: %s",
                    meta.entity_type,
                    str(exc),
                )

        return cleared_count

    def _resolve_cache_args(
        self,
        meta: Optional[CacheEntityMeta],
        *,
        entity_keys: Optional[Dict[str, Any]] = None,
        entity: Any = None,
        identifier: Any = None,
    ) -> Optional[List[Any]]:
        """Resolve cache arguments using only generic parameters."""
        if meta is None:
            return None

        resolved_args: List[Any] = []
        for token in meta.cache_keys:
            value = self._resolve_cache_token(
                token=token,
                entity_keys=entity_keys,
                entity=entity,
                identifier=identifier,
            )
            if not _has_value(value):
                return None
            resolved_args.append(value)

        return resolved_args

    def _resolve_cache_token(
        self,
        *,
        token: str,
        entity_keys: Optional[Dict[str, Any]] = None,
        entity: Any = None,
        identifier: Any = None,
    ) -> Any:
        """
        Resolve cache token values from available sources.

        Args:
            token: Token to resolve (e.g., "context:tenant_id", "key:uuid")
            entity_keys: Dictionary of key-value pairs for resolution
            entity: Entity object with attributes
            identifier: Direct identifier value
        """

        # Handle context tokens
        if token.startswith("context:"):
            context_attribute = token[8:]  # Remove "context:" prefix
            return self._resolve_attribute_value(
                attribute_name=context_attribute,
                entity_keys=entity_keys,
                entity=entity,
            )

        # Handle identifier token
        if token == "identifier":
            return identifier if _has_value(identifier) else None

        # Handle key tokens
        if token.startswith("key:"):
            key_name = token[4:]
            return self._resolve_attribute_value(
                attribute_name=key_name,
                entity_keys=entity_keys,
                entity=entity,
                identifier=identifier,
            )

        # Handle attr tokens
        if token.startswith("attr:"):
            attr_name = token[5:]
            return self._resolve_attribute_value(
                attribute_name=attr_name,
                entity_keys=entity_keys,
                entity=entity,
                identifier=identifier,
            )

        # Return literal token as-is
        return token

    def _resolve_attribute_value(
        self,
        *,
        attribute_name: str,
        entity_keys: Optional[Dict[str, Any]] = None,
        entity: Any = None,
        identifier: Any = None,
    ) -> Any:
        """Generic attribute value resolution."""

        # Try from entity_keys dictionary (highest priority)
        if entity_keys and _has_value(entity_keys.get(attribute_name)):
            return entity_keys.get(attribute_name)

        # Try from entity object
        if entity is not None and hasattr(entity, attribute_name):
            candidate = getattr(entity, attribute_name)
            if _has_value(candidate):
                return candidate

        # Use identifier as fallback for key: and attr: tokens
        if identifier is not None and _has_value(identifier):
            return identifier

        return None

    def _get_entity_meta(self, entity_type: str, custom_options: Optional[Dict[str,Any]] = None) -> Optional[CacheEntityMeta]:
        if custom_options is not None and custom_options.get("custom_getter"):
            entity_meta_key = f"{entity_type}_{custom_options['custom_getter']}"
        else:
            entity_meta_key = f"{entity_type}"

        meta = self._entity_registry.get(entity_meta_key)
        if meta is None:
            if not self._config.get_cache_entity_config:
                return None
            config_map = self._config.get_cache_entity_config()
            if not config_map:
                return None
            config_entry = config_map.get(entity_type)
            if not config_entry:
                return None
            
            getter_name = custom_options.get("custom_getter") if custom_options is not None and custom_options.get("custom_getter") else config_entry.get("getter", f"get_{entity_type}")
            list_resolver_path = custom_options.get("custom_list_resolver") if custom_options is not None and custom_options.get("custom_list_resolver") else config_entry.get("list_resolver")
            cache_keys = custom_options.get("custom_cache_keys") if custom_options is not None and custom_options.get("custom_cache_keys") else config_entry.get("cache_keys", [])

            meta = CacheEntityMeta(
                entity_type=entity_type,
                module=config_entry["module"],
                model_class_name=config_entry.get("model_class"),
                getter_name=getter_name,
                list_resolver_path=list_resolver_path,
                cache_keys=cache_keys,
            )
            self._entity_registry[entity_type] = meta

        if meta.module_ref is None:
            meta.module_ref = import_module(meta.module)

        if meta.getter is None:
            meta.getter = getattr(meta.module_ref, meta.getter_name, None)

        if meta.model_class is None and meta.model_class_name:
            meta.model_class = getattr(meta.module_ref, meta.model_class_name, None)

        if meta.list_resolver is None and meta.list_resolver_path:
            resolver_module, resolver_name = meta.list_resolver_path.rsplit(".", 1)
            resolver_module_ref = import_module(resolver_module)
            meta.list_resolver = getattr(resolver_module_ref, resolver_name, None)

        return meta

    def _build_queries_module_path(self, module_hint: Optional[str]) -> Optional[str]:
        if not module_hint:
            return None
        if "." in module_hint:
            return module_hint
        base = self._config.queries_module_base
        if not base:
            return module_hint
        if not base.endswith("."):
            base = f"{base}."
        return f"{base}{module_hint}"


__all__ = [
    "CacheEntityMeta",
    "CacheConfigResolvers",
    "CascadingCachePurger",
]
