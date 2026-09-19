"""Chat: ask your archive a question and get an answer with receipts.

Layer: L8b - the engine behind the Chat tab. **Qt-free, and never imports
`app.ui`**: the tab depends on this package, not the other way round.

    from app.chat.engine import ChatEngine
    from app.chat.types import ChatTurn, Receipt, NarrationEvent, TokenEvent, ShelfEvent

The modules, in the order a question passes through them:

    router      what kind of question is it                  (section 1a)
    plan        question -> searches; was that enough        (1b)
    context     what the model is shown, and how much        (1c)
    aggregate   counts and lists, computed by SQL            (1d)
    absence     "nothing found", said honestly               (1e)
    prompts     the words said to a model
    verify      no sentence without a receipt                (2)
    engine      the loop that ties them together
    llm         the one seam to a model (Ollama, streaming)
    roles       which model plays which part                 (4d)
    config      the chat tunables
    sessions    saving conversations                         (3d)
    evaluate    the measurement harness behind `evaluate --chat`   (4)
    testing     FakeLLM and a keyword-only search engine, for tests

Importing this package imports nothing heavy; import the module you need.
"""

from app.chat.types import (
    ChatTurn,
    NarrationEvent,
    Receipt,
    ShelfEvent,
    TokenEvent,
)

__all__ = ["ChatTurn", "NarrationEvent", "Receipt", "ShelfEvent", "TokenEvent"]
