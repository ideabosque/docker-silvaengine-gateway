#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import functools
import traceback
import uuid
from typing import Any, Dict

import pendulum
from graphene import ResolveInfo
from pynamodb.attributes import (
    ListAttribute,
    MapAttribute,
    NumberAttribute,
    UnicodeAttribute,
    UTCDateTimeAttribute,
)
from pynamodb.indexes import AllProjection, LocalSecondaryIndex
from silvaengine_constants import LLMUserRole, SwitchStatus
from silvaengine_dynamodb_base import (
    BaseModel,
    delete_decorator,
    insert_update_decorator,
)
from silvaengine_utility import convert_decimal_to_number, method_cache
from tenacity import retry, stop_after_attempt, wait_exponential


class AgentUuidIndex(LocalSecondaryIndex):
    """
    LSI for querying agents by agent_uuid within a partition.

    MIGRATION NOTE: Updated from endpoint_id to partition_key as hash_key.
    All LSIs share the same partition_key as the main table.
    This allows efficient queries like:
        AgentModel.agent_uuid_index.query(partition_key, AgentModel.agent_uuid == uuid)
    """

    class Meta:
        billing_mode = "PAY_PER_REQUEST"
        # All attributes are projected
        projection = AllProjection()
        index_name = "agent_uuid-index"

    partition_key = UnicodeAttribute(hash_key=True)  # MIGRATED: was endpoint_id
    agent_uuid = UnicodeAttribute(range_key=True)


class UpdatedAtIndex(LocalSecondaryIndex):
    """
    LSI for querying agents by updated_at within a partition.

    MIGRATION NOTE: Updated from endpoint_id to partition_key as hash_key.
    Enables time-range queries like:
        AgentModel.updated_at_index.query(partition_key, AgentModel.updated_at > start_time)
    """

    class Meta:
        billing_mode = "PAY_PER_REQUEST"
        # All attributes are projected
        projection = AllProjection()
        index_name = "updated_at-index"

    partition_key = UnicodeAttribute(hash_key=True)  # MIGRATED: was endpoint_id
    updated_at = UnicodeAttribute(range_key=True)


class AgentModel(BaseModel):
    """
    Agent Model - Reference Implementation for partition_key Migration

    MIGRATION PATTERN:
    1. Hash key changed from endpoint_id to partition_key (composite key)
    2. Added denormalized endpoint_id and part_id fields (for reference/debugging)
    3. Updated all LSI indexes to use partition_key as hash_key
    4. partition_key assembled in main.py: f"{endpoint_id}#{part_id}"

    USAGE:
    - Direct lookup: AgentModel.get(partition_key, agent_version_uuid)
    - Query by agent_uuid: AgentModel.agent_uuid_index.query(partition_key, ...)
    - Query by time: AgentModel.updated_at_index.query(partition_key, ...)

    IMPORTANT:
    - All 3 fields (partition_key, endpoint_id, part_id) must be set on insert/update
    - Extract endpoint_id/part_id from info.context, NOT by parsing partition_key
    - This model serves as the reference for migrating 8 remaining models
    """

    class Meta(BaseModel.Meta):
        table_name = "aace-agents"

    # Primary Key (MIGRATED)
    # Format: "endpoint_id#part_id" (was: endpoint_id)
    partition_key = UnicodeAttribute(hash_key=True)
    agent_version_uuid = UnicodeAttribute(range_key=True)

    # Denormalized attributes (NEW - for reference/debugging only, no indexes needed)
    endpoint_id = UnicodeAttribute()  # Platform partition (e.g., "aws-prod-us-east-1")
    part_id = UnicodeAttribute()  # Business partition (e.g., "acme-corp")

    # Other attributes
    agent_uuid = UnicodeAttribute()
    agent_name = UnicodeAttribute()
    agent_description = UnicodeAttribute(null=True)
    llm_provider = UnicodeAttribute()
    llm_name = UnicodeAttribute()
    instructions = UnicodeAttribute(null=True)
    configuration = MapAttribute()
    mcp_server_uuids = ListAttribute(null=True)
    variables = ListAttribute(null=True, of=MapAttribute)
    num_of_messages = NumberAttribute(default=10)
    tool_call_role = UnicodeAttribute(default=LLMUserRole.DEVELOPER.value)
    flow_snippet_version_uuid = UnicodeAttribute(null=True)
    status = UnicodeAttribute(default=SwitchStatus.ACTIVE.value)
    updated_by = UnicodeAttribute()
    created_at = UTCDateTimeAttribute()
    updated_at = UTCDateTimeAttribute()

    # Indexes (all LSI - share same partition_key)
    # MIGRATION NOTE: All LSIs updated to use partition_key instead of endpoint_id
    agent_uuid_index = AgentUuidIndex()  # Query by agent_uuid within partition
    updated_at_index = UpdatedAtIndex()  # Query by time range within partition
