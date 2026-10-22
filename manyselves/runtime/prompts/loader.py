"""Prompt loader — loads complete agent prompts with shared context."""

from pathlib import Path
from typing import Any, Final

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
    _CAPABILITIES_DIR: Final = Path(__file__).parent.parent.parent / "capabilities"

    def __init__(self, agents_dir: Path | None = None, capabilities_dir: Path | None = None):
        """Initialize prompt loader.

        Args:
            agents_dir: Optional custom directory for agent templates.
                       Defaults to manyselves/templates/agents/.
        """
        self._agents_dir = Path(agents_dir) if agents_dir else self._AGENTS_DIR
        # Custom agent directories (tests) get no Capability parts unless asked.
        self._capabilities_dir = (
            Path(capabilities_dir)
            if capabilities_dir
            else (None if agents_dir else self._CAPABILITIES_DIR)
        )
        self._cache: dict[str, tuple[Any, str]] = {}
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
        fragments = self._capability_fragments(filename)
        signature = (
            filepath.stat().st_mtime_ns if filepath.exists() else None,
            tuple((str(path), path.stat().st_mtime_ns) for path in fragments),
        )

        cached = self._cache.get(agent_type)
        if cached and cached[0] == signature:
            return cached[1]

        if not filepath.exists():
            logger.warning("Prompt file not found: {}, using fallback", filepath)
            prompt = self._get_fallback_prompt(agent_type)
        else:
            content = filepath.read_text(encoding="utf-8")
            prompt = content.strip()
        # Each installed Capability may add its own part for this Agent, in a
        # file of the same name in its package; the kernel file stays generic.
        parts = [path.read_text(encoding="utf-8").strip() for path in fragments]
        prompt = "\n\n".join([prompt, *(part for part in parts if part)])

        self._cache[agent_type] = (signature, prompt)
        return prompt

    def _capability_fragments(self, filename: str) -> list[Path]:
        if self._capabilities_dir is None or not self._capabilities_dir.is_dir():
            return []
        return sorted(self._capabilities_dir.glob(f"*/{filename}"))

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
        return (
            f"You are the {agent_type} agent. Follow the current task definition "
            "and use only the available tools."
        )

    def reload(self) -> None:
        """Clear cache — useful when prompts are modified at runtime."""
        self._cache.clear()
        self._shared_cache = None
        logger.info("Prompt cache cleared")
