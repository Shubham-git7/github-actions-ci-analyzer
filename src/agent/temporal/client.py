from temporalio.client import Client

from agent.config import Settings
from agent.temporal.codec import data_converter


async def connect(s: Settings) -> Client:
    return await Client.connect(s.temporal_address, namespace=s.temporal_namespace, data_converter=data_converter(s.codec_key))
