import hashlib
import math
import re

from langchain_core.embeddings import Embeddings
from langchain_core.runnables import RunnableLambda

from backend.schemas import MerchantCategorizationBatch, MerchantCategory


class FakeLLM:
    def __init__(self):
        self.answers = {}
        self.calls = []

    def with_structured_output(self, schema):
        async def answer(messages):
            self.calls.append({"schema": schema.__name__, "prompt": str(messages)})
            reply = self.answers[schema.__name__]
            if isinstance(reply, list):
                reply = reply.pop(0) if len(reply) > 1 else reply[0]
            if callable(reply):
                reply = reply(messages)
            if isinstance(reply, Exception):
                raise reply
            return reply

        return RunnableLambda(answer)

    def count(self, schema_name):
        return len([call for call in self.calls if call["schema"] == schema_name])

    def prompts(self, schema_name):
        return [call["prompt"] for call in self.calls if call["schema"] == schema_name]


def categorizer(answers: dict):
    def reply(messages):
        merchants = re.findall(r"<merchant>(.*?)</merchant>", messages[-1][1])
        items = [
            MerchantCategory(merchant=merchant, category=answers[merchant][0], confidence=answers[merchant][1])
            for merchant in merchants
            if merchant in answers
        ]
        return MerchantCategorizationBatch(items=items)

    return reply


class FakeEmbeddings(Embeddings):
    def vector(self, text):
        values = [0.0] * 256
        for word in re.findall(r"\w+", text.lower()):
            values[int(hashlib.md5(word.encode()).hexdigest(), 16) % 256] += 1
        length = math.sqrt(sum(v * v for v in values)) or 1
        return [v / length for v in values]

    def embed_documents(self, texts):
        return [self.vector(text) for text in texts]

    def embed_query(self, text):
        return self.vector(text)
