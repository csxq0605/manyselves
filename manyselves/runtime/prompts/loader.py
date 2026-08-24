"""Prompt loader — loads complete agent prompts with shared context."""

from pathlib import Path
from typing import Final

from loguru import logger


class PromptLoader:
    """Load agent prompts.

    Each agent prompt file is a single Markdown file.  The full content is
    loaded and returned every time — there is no identity/full split.
    Shared context (Common.md) and per-agent skill summaries are assembled
    by the caller, not here.
    """

    # Template directory paths
    _BASE_DIR: Final = Path(__file__).parent.parent.parent / "templates"
    _AGENTS_DIR: Final = _BASE_DIR / "agents"

    def __init__(self, agents_dir: Path | None = None):
        """Initialize prompt loader.

        Args:
            agents_dir: Optional custom directory for agent templates.
                       Defaults to manyselves/templates/agents/.
        """
        self._agents_dir = Path(agents_dir) if agents_dir else self._AGENTS_DIR
        self._cache: dict[str, tuple[int | None, str]] = {}
        self._shared_cache: tuple[int | None, str | None] | None = None

    def load_prompt(self, agent_type: str) -> str:
        """Load complete agent prompt.

        Args:
            agent_type: Runtime agent identifier. The GUI runtime uses ``main``.

        Returns:
            Complete prompt file content.

        Raises:
            FileNotFoundError: If prompt file not found.
        """
        filename = self._get_filename(agent_type)
        filepath = self._agents_dir / filename
        mtime_ns = filepath.stat().st_mtime_ns if filepath.exists() else None

        cached = self._cache.get(agent_type)
        if cached and cached[0] == mtime_ns:
            return cached[1]

        if not filepath.exists():
            logger.warning("Prompt file not found: {}, using fallback", filepath)
            prompt = self._get_fallback_prompt(agent_type)
        else:
            content = filepath.read_text(encoding="utf-8")
            prompt = content.strip()

        self._cache[agent_type] = (mtime_ns, prompt)
        return prompt

    def load_shared_context(self) -> str | None:
        """Load shared prompts for all agents.

        Returns:
            Shared context content, or None if file not found.
        """
        path = self._agents_dir / "Common.md"
        mtime_ns = path.stat().st_mtime_ns if path.exists() else None

        if self._shared_cache and self._shared_cache[0] == mtime_ns:
            return self._shared_cache[1]

        if not path.exists():
            logger.debug("Shared context file not found: {}", path)
            self._shared_cache = (None, None)
            return None

        content = path.read_text(encoding="utf-8").strip()
        shared = content or None
        self._shared_cache = (mtime_ns, shared)
        return shared

    def get_signature(self, agent_type: str) -> tuple[int | None, int | None]:
        """Return a file-based signature for prompt cache invalidation."""
        filename = self._get_filename(agent_type)
        prompt_path = self._agents_dir / filename
        shared_path = self._agents_dir / "Common.md"
        prompt_sig = prompt_path.stat().st_mtime_ns if prompt_path.exists() else None
        shared_sig = shared_path.stat().st_mtime_ns if shared_path.exists() else None
        return prompt_sig, shared_sig

    def _get_filename(self, agent_type: str) -> str:
        normalized = agent_type.lower().replace("-", "_").replace(" ", "_")
        mapping = {"main": "main_agent.md"}
        return mapping.get(normalized, f"{normalized}_agent.md")

    def _get_fallback_prompt(self, agent_type: str) -> str:
        if agent_type.lower() == "main":
            return "你是 Manyselves Main Agent。根据当前项目与可用工具协助用户。"
        return f"You are the {agent_type} role in the power-distribution reporting workflow."

    def reload(self) -> None:
        """Clear cache — useful when prompts are modified at runtime."""
        self._cache.clear()
        self._shared_cache = None
        logger.info("Prompt cache cleared")
