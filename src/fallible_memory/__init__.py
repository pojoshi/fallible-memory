"""fallible-memory: a memory layer that knows it might be wrong."""

from .agent import ask, ask_text, answer_from
from .embedders import HashingEmbedder, make_embedder
from .models import AccessContext, AgentAnswer, Fact, Retrieval, WriteResult
from .service import MemoryService
from .store import InMemoryStore

__all__ = [
    "AccessContext", "AgentAnswer", "Fact", "HashingEmbedder", "InMemoryStore", "MemoryService",
    "Retrieval", "WriteResult", "answer_from", "ask", "ask_text", "make_embedder",
]
__version__ = "0.1.0"
