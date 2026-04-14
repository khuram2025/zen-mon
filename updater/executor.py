"""Manifest step executor with rollback support."""

import logging
from pathlib import Path

from .config import AgentConfig

logger = logging.getLogger("zenplus.updater")


class ExecutionError(Exception):
    """Raised when a step fails and rollback is needed."""


# Step type → handler function mapping
_STEP_HANDLERS = {}


def step_handler(step_type: str):
    """Decorator to register a step handler."""
    def decorator(func):
        _STEP_HANDLERS[step_type] = func
        return func
    return decorator


def _load_step_handlers():
    """Import all step handler modules to trigger registration."""
    from .steps import (
        apply_code,
        backup,
        build_dashboard,
        health_check,
        install_binary,
        install_config,
        install_systemd,
        os_package,
        pip_install,
        run_hook,
        run_migration,
        service_control,
    )


def execute_step(step: dict, extract_dir: str, cfg: AgentConfig) -> None:
    """Execute a single manifest step."""
    step_type = step.get("type", "")
    handler = _STEP_HANDLERS.get(step_type)

    if not handler:
        raise ExecutionError(f"Unknown step type: {step_type}")

    logger.info("Executing step: %s %s", step_type, _step_summary(step))
    try:
        handler(step, extract_dir, cfg)
        logger.info("Step completed: %s", step_type)
    except Exception as e:
        logger.error("Step failed: %s — %s", step_type, e)
        raise ExecutionError(f"Step '{step_type}' failed: {e}") from e


def _step_summary(step: dict) -> str:
    """Generate a short summary of a step for logging."""
    step_type = step.get("type", "")
    if step_type in ("stop_services", "start_services"):
        return f"[{', '.join(step.get('services', []))}]"
    if step_type == "run_migration":
        return f"[{step.get('engine', '?')}: {step.get('file', '?')}]"
    if step_type == "health_check":
        return f"[{step.get('url', '?')}]"
    if step_type == "install_binary":
        return f"[{step.get('source', '?')} → {step.get('dest', '?')}]"
    return ""


def execute_manifest(
    manifest: dict, extract_dir: str, cfg: AgentConfig
) -> None:
    """Execute all steps in a manifest, with rollback on failure.

    Raises ExecutionError if the update fails (rollback will have been attempted).
    """
    _load_step_handlers()

    steps = manifest.get("steps", [])
    rollback_steps = manifest.get("rollback_steps", [])
    completed_steps = []

    logger.info("Executing %d update steps", len(steps))

    for i, step in enumerate(steps, 1):
        try:
            logger.info("--- Step %d/%d ---", i, len(steps))
            execute_step(step, extract_dir, cfg)
            completed_steps.append(step)
        except ExecutionError as e:
            logger.error("Step %d/%d failed, initiating rollback ...", i, len(steps))

            # Run rollback steps
            if rollback_steps:
                _execute_rollback(rollback_steps, extract_dir, cfg)
            else:
                logger.warning("No rollback steps defined in manifest")

            raise ExecutionError(
                f"Update failed at step {i}/{len(steps)} ({step.get('type')}): {e}"
            ) from e

    logger.info("All %d steps completed successfully", len(steps))


def _execute_rollback(
    rollback_steps: list[dict], extract_dir: str, cfg: AgentConfig
) -> None:
    """Execute rollback steps. Errors are logged but don't stop the rollback."""
    logger.warning("Running %d rollback steps ...", len(rollback_steps))

    for i, step in enumerate(rollback_steps, 1):
        try:
            logger.info("--- Rollback step %d/%d ---", i, len(rollback_steps))
            execute_step(step, extract_dir, cfg)
        except Exception as e:
            logger.error(
                "Rollback step %d failed (continuing): %s — %s",
                i,
                step.get("type"),
                e,
            )

    logger.warning("Rollback complete")
