class DeterministicFakeProvider:
    """Network-free provider used by release harnesses that need stable text."""

    async def complete(self, prompt: str) -> str:
        return f"FAKE:{prompt.strip()}"
