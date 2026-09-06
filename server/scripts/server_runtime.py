"""Canonical shared runtime state for modular Sentient Sands server modules."""

from types import SimpleNamespace
import logging


runtime = SimpleNamespace(
    models_config={},
    providers_config={},
    current_model_key="player2-default",
    player2_session_key=None,
    debug_logger=logging.getLogger("kenshi_debug"),
)


def set_model_configs(models_config=None, providers_config=None):
    runtime.models_config = models_config if models_config is not None else {}
    runtime.providers_config = providers_config if providers_config is not None else {}


def set_current_model_key(model_key):
    runtime.current_model_key = model_key or "player2-default"


def set_player2_session_key(session_key):
    runtime.player2_session_key = session_key


def set_debug_logger(logger):
    runtime.debug_logger = logger if logger is not None else logging.getLogger("kenshi_debug")

