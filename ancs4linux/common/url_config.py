import json
import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote_plus

log = logging.getLogger(__name__)

_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "ancs4linux"
_CONFIG_FILE = _CONFIG_DIR / "open_url.json"


def load_config() -> Dict[str, str]:
    if not _CONFIG_FILE.exists():
        return {}
    with open(_CONFIG_FILE) as f:
        return json.load(f)


def save_config(config: Dict[str, str]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def resolve_target(app_id: str, app_name: str) -> str:
    """Return the configured URL or app:// target for an app, or a Google fallback."""
    config = load_config()
    return (
        config.get(app_id)
        or config.get(app_name)
        or f"https://www.google.com/search?q={quote_plus(app_name)}&btnI=1"
    )


def open_target(app_id: str, app_name: str) -> None:
    """Open the URL or launch the Linux app configured for this notification's app."""
    target = resolve_target(app_id, app_name)
    if target.startswith("app://"):
        command = shlex.split(target[len("app://"):])
        log.debug(f"Launching app command: {command}")
        subprocess.Popen(command)
    else:
        log.debug(f"Opening URL: {target}")
        subprocess.Popen(["xdg-open", target])


def set_mapping(key: str, target: str) -> None:
    config = load_config()
    config[key] = target
    save_config(config)


def remove_mapping(key: str) -> bool:
    config = load_config()
    if key not in config:
        return False
    del config[key]
    save_config(config)
    return True
