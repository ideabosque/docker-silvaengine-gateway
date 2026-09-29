#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

from typing import Any, Dict, Optional

import pendulum
from pynamodb.attributes import MapAttribute, UnicodeAttribute, UTCDateTimeAttribute
from pynamodb.indexes import AllProjection, GlobalSecondaryIndex
from pynamodb.pagination import ResultIterator

from silvaengine_constants import SwitchStatus

from ..model import BaseModel


class ConnectionIdIndex(GlobalSecondaryIndex):
    """
    This class represents a local secondary index
    """

    class Meta:
        billing_mode = "PAY_PER_REQUEST"
        projection = AllProjection()
        index_name = "connection_id-index"

    connection_id = UnicodeAttribute(hash_key=True)


class WSSConnectionModel(BaseModel):
    class Meta(BaseModel.Meta):
        table_name = "se-wss-connections"

    endpoint_id = UnicodeAttribute(hash_key=True)
    connection_id = UnicodeAttribute(range_key=True)
    api_key = UnicodeAttribute()
    area = UnicodeAttribute()
    data = MapAttribute(default=dict)
    url_parameters = MapAttribute(default=dict)
    status = UnicodeAttribute(default=SwitchStatus.ACTIVE.name)
    created_at = UTCDateTimeAttribute()
    updated_at = UTCDateTimeAttribute()
    connect_id_index = ConnectionIdIndex()

    @classmethod
    def store(
        cls,
        endpoint_id: str,
        connection_id: str,
        api_key: str,
        url_parameters: Dict[str, Any],
        area: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Save a WSS connection model to DynamoDB.
        :param endpoint_id: The ID of the endpoint.
        :param connection_id: The ID of the connection.
        :param api_key: The API key.
        :param area: The area.
        :param data: The connection data.
        """
        try:
            now = pendulum.now("UTC")

            return WSSConnectionModel(
                endpoint_id,
                connection_id,
                **{
                    "api_key": api_key,
                    "url_parameters": url_parameters,
                    "area": area,
                    "data": data,
                    "updated_at": now,
                    "created_at": now,
                },
            ).save()
        except Exception as e:
            raise ValueError(f"Failed to save WSS connection: {str(e)}")

    @classmethod
    def fetch(cls, endpoint_id: str, connection_id: str) -> "WSSConnectionModel":
        """
        Get a WSS connection model from DynamoDB.
        :param endpoint_id: The ID of the endpoint.
        :param connection_id: The ID of the connection.
        :return: The WSS connection model.
        """
        try:
            return WSSConnectionModel.get(endpoint_id, connection_id)
        except Exception as e:
            raise ValueError(f"Failed to get WSS connection: {str(e)}")

    @classmethod
    def find(
        cls,
        connection_id: str,
        index_name: Optional[str] = "connect_id_index",
    ) -> ResultIterator["WSSConnectionModel"]:
        """
        Get all WSS connection models from DynamoDB for a given connection.
        :param connection_id: The ID of the connection.
        :return: A list of WSS connection models.
        """
        try:
            index_name = str(index_name).strip() if index_name else ""

            if index_name:
                return getattr(WSSConnectionModel, index_name).query(connection_id)

            return WSSConnectionModel.query(connection_id)
        except Exception as e:
            raise ValueError(f"Failed to get WSS connections: {str(e)}")

    @classmethod
    def cleanup_connections(cls, endpoint_id: str, expires_in_minutes: int) -> None:
        """
        Get all WSS connection models from DynamoDB for a given endpoint.
        :param endpoint_id: The ID of the endpoint.
        :param range_condition: The range key condition.
        :param cutoff_time: The cutoff time.
        :return: A list of WSS connection models.
        """
        try:
            threshold_time = pendulum.now("UTC").subtract(
                minutes=int(expires_in_minutes)
            )
            connections = WSSConnectionModel.query(
                hash_key=endpoint_id,
                filter_condition=WSSConnectionModel.updated_at < threshold_time,
            )

            # Iterate through and delete matching connections
            for connection in connections:
                connection.delete()
        except Exception as e:
            raise ValueError(f"Failed to get WSS connections: {str(e)}")
