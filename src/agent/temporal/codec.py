"""Payload encryption: workflow history holds ciphertext, so a database dump or a shared Temporal cluster
does not expose CI log excerpts, diffs or analysis text. The key never leaves the api and worker processes."""
import asyncio
from collections.abc import Sequence

from cryptography.fernet import Fernet
from temporalio.api.common.v1 import Payload
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import DataConverter, PayloadCodec

ENCODING = b"binary/encrypted"


class EncryptionCodec(PayloadCodec):
    def __init__(self, key: str | bytes):
        self._f = Fernet(key)

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [Payload(metadata={"encoding": ENCODING}, data=await asyncio.to_thread(self._f.encrypt, p.SerializeToString()))
                for p in payloads]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        out = []
        for p in payloads:
            if p.metadata.get("encoding") == ENCODING:
                q = Payload()
                q.ParseFromString(await asyncio.to_thread(self._f.decrypt, p.data))
                out.append(q)
            else:
                out.append(p)
        return out


def data_converter(key: str = "") -> DataConverter:
    """Pydantic types in/out; encrypted when a key is configured (always, in the compose stack)."""
    if not key:
        return pydantic_data_converter
    import dataclasses

    return dataclasses.replace(pydantic_data_converter, payload_codec=EncryptionCodec(key))
