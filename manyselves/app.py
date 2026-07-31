"""Main application entry point."""

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Annotated

# Force UTF-8 encoding for all I/O operations
# This fixes Chinese character display issues on Windows systems
if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

import typer
from loguru import logger
from PyQt6.QtWidgets import QApplication, QDialog
from rich.console import Console

from .application.backend_api import BackendAPIImpl as BackendAPIImpl
from .application.errors import RuntimeStartupError
from .application.runtime_host import RuntimeHost
from .branding import APP_ICON_PATH, DESCRIPTOR_ZH, PRODUCT_NAME
from .config import ConfigManager
from .core.loops import LoopManager
from .core.project_structure import ensure_project_structure
from .gui import MainWindow
from .utils import log_exception, setup_exception_handler, setup_logging

console = Console()
app = typer.Typer(
    name="manyselves",
    help=f"{PRODUCT_NAME} — {DESCRIPTOR_ZH}",
    rich_markup_mode="rich",
    no_args_is_help=True,
)


# Known noisy macOS platform lines (byte patterns) to drop from stderr.
_BLOCK_STDERR_PATTERNS = (
    b"IMKCFRunLoopWakeUpReliable",
    b"TSM AdjustCapsLockLEDForKeyTransitionHandling",
    b"_ISSetPhysicalKeyboardCapsLockLED",
)


def _install_stderr_filter() -> None:
    """Redirect OS-level stderr through a line filter on macOS.

    Qt (``qWarning``) and macOS system frameworks (IMK / ``NSLog``) write
    directly to the stderr file descriptor, bypassing Python's ``sys.stderr``
    object — so wrapping ``sys.stderr`` cannot suppress them.  We instead point
    fd 2 at a pipe and filter the byte stream on a background thread, dropping
    known noisy platform lines before forwarding survivors to the real stderr.

    Tool subprocesses capture their own stderr (``stderr=PIPE``), so they are
    unaffected.  A detached relaunch child inherits fd 2 (this pipe); once the
    parent exits the child's forward target is gone — on that broken-pipe error
    the pump keeps draining-and-discarding so the child never blocks on a full
    pipe (it simply stops forwarding, which is harmless for a relaunched app).
    """
    import os
    import threading

    if sys.platform != "darwin":
        return

    try:
        real_stderr_fd = os.dup(2)
        read_fd, write_fd = os.pipe()
        os.dup2(write_fd, 2)
        os.close(write_fd)
    except OSError:
        return  # Don't let filter setup break app startup.

    def _pump() -> None:
        buf = b""
        forward_dead = False
        while True:
            try:
                chunk = os.read(read_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            if forward_dead:
                continue  # keep draining so the pipe can never fill / block.
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if any(pat in line for pat in _BLOCK_STDERR_PATTERNS):
                    continue
                try:
                    os.write(real_stderr_fd, line + b"\n")
                except OSError:
                    forward_dead = True
                    buf = b""
                    break
        # Flush any trailing partial line that lacks a newline.
        if buf and not forward_dead and not any(pat in buf for pat in _BLOCK_STDERR_PATTERNS):
            try:
                os.write(real_stderr_fd, buf)
            except OSError:
                pass
        try:
            os.close(read_fd)
        except OSError:
            pass

    threading.Thread(target=_pump, daemon=True, name="stderr-filter").start()



class ManyselvesApp:
    """Main Manyselves application."""

    def __init__(self, runtime_host: RuntimeHost | None = None):
        """Initialize application."""
        self._runtime_host = runtime_host or RuntimeHost.create()
        self.config_manager = self._runtime_host.config_manager
        self.bus = self._runtime_host.bus
        self.backend = self._runtime_host.backend
        self.loop_manager: LoopManager | None = self._runtime_host.loop_manager
        self.main_window: MainWindow | None = None
        self._qt_app: QApplication | None = None
        self._interrupted = False

    async def startup(self, workspace: Path) -> bool:
        """Startup application. Returns True if successful.

        Args:
            workspace: Project workspace path.

        Returns:
            True if startup successful, False otherwise.
        """
        try:
            await self._runtime_host.start(workspace)
        except RuntimeStartupError as exc:
            if exc.code == "NO_PROVIDER_KEYS":
                return False
            raise

        self.loop_manager = self._runtime_host.loop_manager
        return True

    def _ensure_project_structure(self, workspace: Path) -> None:
        """Ensure project directory structure exists.

        Args:
            workspace: Project workspace path.
        """
        ensure_project_structure(workspace)

        logger.debug("Ensured project structure in: {}", workspace)

    async def shutdown(self) -> None:
        """Shutdown application."""
        await self._runtime_host.stop()
        self.loop_manager = self._runtime_host.loop_manager
        logger.info("Application shut down")

    def _check_interrupt(self) -> None:
        if self._interrupted:
            logger.info("Shutting down via interrupt check timer...")
            from PyQt6.QtWidgets import QApplication
            QApplication.closeAllWindows()
            self._qt_app.quit()

    def run_gui(self, qt_app: QApplication, project: Path | None = None) -> None:
        """Run GUI application."""
        import threading

        self._qt_app = qt_app

        # Setup signal handler for Ctrl+C
        def _handle_interrupt(*_):
            logger.info("Interrupt received, shutting down...")
            self._interrupted = True
            qt_app.quit()

        signal.signal(signal.SIGINT, _handle_interrupt)
        # Windows also supports SIGBREAK
        if hasattr(signal, 'SIGBREAK'):
            signal.signal(signal.SIGBREAK, _handle_interrupt)

        # Timer to check for interrupts periodically (Qt event loop blocks signals)
        from PyQt6.QtCore import QTimer
        self._interrupt_timer = QTimer()
        self._interrupt_timer.timeout.connect(self._check_interrupt)
        self._interrupt_timer.start(100)  # Check every 100ms

        # QApplication already created in main(), get the instance
        app = QApplication.instance()

        wants_tutorial = False
        workspace: Path | None = None

        if project is not None:
            # Direct project open (e.g. workspace switch relaunch):
            # skip welcome guide and project selection dialog.
            workspace = Path(project).expanduser().resolve()
            logger.info("Opening project directly: {}", workspace)
        else:
            # ── Phase 1: Pre-project welcome guide ──
            from .gui.onboarding import show_pre_project_guide
            wants_tutorial = show_pre_project_guide()

            # Show project selection dialog first
            from .gui.project_dialog import ProjectDialog

            project_dialog = ProjectDialog(self.config_manager)

            def on_project_selected(path: Path):
                nonlocal workspace
                workspace = path

            project_dialog.project_selected.connect(on_project_selected)

            # Show project dialog
            result = project_dialog.exec()

            # The "新手提示" button may have re-enabled the tutorial from inside
            # the project dialog; honor that choice for the Phase 2 tutorial.
            wants_tutorial = wants_tutorial or project_dialog.wants_tutorial

            if result != QDialog.DialogCode.Accepted:
                logger.info("Project selection cancelled")
                sys.exit(0)

            if workspace is None:
                logger.error("No workspace selected")
                sys.exit(1)

        # Create a dedicated async event loop for the backend
        self._async_loop = asyncio.new_event_loop()

        # Run startup in the async loop
        try:
            success = self._async_loop.run_until_complete(self.startup(workspace))
            if not success:
                logger.error("Failed to start application")
                sys.exit(1)
        except Exception as e:
            log_exception("Error during startup", e)
            sys.exit(1)

        # Keep the async loop running in a background thread so
        # run_coroutine_threadsafe works from the Qt GUI thread.
        def _run_loop():
            asyncio.set_event_loop(self._async_loop)
            self._async_loop.run_forever()

        self._loop_thread = threading.Thread(target=_run_loop, daemon=True)
        self._loop_thread.start()

        # Activate debug mode for agents specified via --debug-agent
        for agent in getattr(self, "_debug_agents_on_start", []):
            self.set_agent_debug_mode(agent, enabled=True)
            logger.info("Debug mode activated for {} (via CLI)", agent)

        # Create main window with selected workspace
        self.main_window = MainWindow(
            backend=self.backend,
            workspace=workspace,
            debug_agents=list(getattr(self, "_debug_agents_on_start", [])),
        )
        self.main_window.set_async_loop(self._async_loop)
        self.main_window.prepare_initial_render()
        self.main_window.show()

        # ── Phase 2: Post-project tutorial (only if user chose "new user" in Phase 1) ──
        if wants_tutorial:
            from .gui.onboarding import show_onboarding
            show_onboarding(self.main_window)

        exit_code = app.exec()

        # Graceful shutdown: run async cleanup in the loop
        future = asyncio.run_coroutine_threadsafe(self.shutdown(), self._async_loop)
        try:
            future.result(timeout=5)
        except Exception:
            pass

        # Stop the background event loop
        self._async_loop.call_soon_threadsafe(self._async_loop.stop)
        self._loop_thread.join(timeout=5)

        sys.exit(exit_code)


def _try_sync_presets(silent: bool = False) -> bool:
    """Try to sync presets from cc-switch. Returns True on success."""
    from .core.preset_sync import is_cached, sync_presets

    try:
        if is_cached():
            logger.debug("Presets already cached, skipping auto-sync")
            return True
        n = sync_presets(timeout=10)
        logger.info("Auto-synced {} preset files from cc-switch", n)
        return True
    except Exception as e:
        if not silent:
            logger.warning("Preset sync failed (network/proxy issue): {}", e)
        return False


def _check_dependencies(config_manager: ConfigManager) -> None:
    """Check for optional tool dependencies and log warnings."""
    import shutil

    # MinerU
    cfg = config_manager.config
    check_mineru = (
        hasattr(cfg, "mineru_api")
        and cfg.mineru_api.enabled
        and cfg.mineru_api.validate_on_startup
    )
    if check_mineru and not shutil.which("mineru-open-api"):
        logger.warning(
            "mineru-open-api not found. PDF parsing will be unavailable. "
            "Install: https://mineru.net/ecosystem?tab=cli"
        )


@app.command()
def main(
    debug_agent: Annotated[
        list[str],
        typer.Option(
            "--debug-agent",
            help="在调试模式下启动指定的 Agent（可重复使用）",
        ),
    ] = [],  # noqa: B006
    sync_presets: Annotated[
        bool,
        typer.Option(
            "--sync-presets",
            help="从 cc-switch 仓库同步最新预设模板并退出",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "-v", "--verbose",
            help="输出 DEBUG 级别调试信息",
        ),
    ] = False,
    project: Annotated[
        Path | None,
        typer.Option(
            "--project", "-p",
            help="直接打开指定项目目录（跳过项目选择对话框，用于切换工作区）",
        ),
    ] = None,
) -> None:
    """Manyselves — 由文档定义 Agent 团队的本地工作空间。"""
    _install_stderr_filter()

    # Handle --sync-presets (CLI mode, no GUI needed)
    if sync_presets:
        setup_logging(log_level="INFO", log_to_file=True)
        console.print("[bold cyan]Syncing presets from cc-switch...[/bold cyan]")
        ok = _try_sync_presets(silent=False)
        if ok:
            from .config.presets import load_presets

            presets = load_presets()
            console.print(f"[green]Sync complete. {len(presets)} presets available.[/green]")
        else:
            console.print("[red]Sync failed. Check network/proxy settings.[/red]")
        raise typer.Exit(code=0 if ok else 1)

    # Create Qt application BEFORE any other initialization
    qt_app = QApplication(sys.argv)
    assert QApplication.instance() is not None, "QApplication creation failed"
    qt_app.setApplicationName(PRODUCT_NAME)
    qt_app.setApplicationDisplayName(PRODUCT_NAME)

    # Set application icon
    from PyQt6.QtGui import QIcon

    if APP_ICON_PATH.exists():
        qt_app.setWindowIcon(QIcon(str(APP_ICON_PATH)))

    # Setup logging and exception handling
    log_level = "DEBUG" if verbose else "INFO"
    setup_logging(log_level=log_level, log_to_file=True)
    setup_exception_handler()

    logger.info("{} starting...", PRODUCT_NAME)

    invalid_debug_agents = sorted(set(debug_agent) - {"main"})
    if invalid_debug_agents:
        raise typer.BadParameter(
            f"GUI 运行时只支持 --debug-agent main；无效值: {', '.join(invalid_debug_agents)}"
        )

    app_inst = ManyselvesApp()

    # Store debug agents for activation after loop manager starts
    app_inst._debug_agents_on_start = debug_agent

    # Check optional dependencies
    _check_dependencies(app_inst.config_manager)

    # Auto-sync presets on startup (failure is non-fatal)
    if not _try_sync_presets(silent=True):
        logger.info("Presets not synced — UI sync button available for retry")

    # Check API configuration first
    is_valid, _ = app_inst.config_manager.validate_api_keys()

    if not is_valid:
        from .gui.config_dialog import ConfigDialog

        dialog = ConfigDialog(app_inst.config_manager)
        result = dialog.exec()

        if result != QDialog.DialogCode.Accepted:
            logger.info("Configuration cancelled by user")
            raise typer.Exit(code=0)

        is_valid, _ = app_inst.config_manager.validate_api_keys()
        if not is_valid:
            logger.error("Still no valid API keys after configuration")
            raise typer.Exit(code=1)

    # Run GUI (includes project selection and startup)
    app_inst.run_gui(qt_app, project=project)

    # Shutdown
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(app_inst.shutdown())
    except Exception as e:
        log_exception("Error during shutdown", e)
    finally:
        del qt_app


if __name__ == "__main__":
    app()
