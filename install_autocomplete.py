#!/usr/bin/env python3
"""
Install argcomplete shell autocompletion for cqscout.py.
Detects bash, zsh, fish, tcsh, and powershell and configures each one found.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_NAME = "cqscout.py"
SCRIPT_PATH = (Path(__file__).parent / SCRIPT_NAME).resolve()

# Per-shell: (binary, config files to try in order, completion snippet template)
# {name} is replaced with SCRIPT_NAME, {path} with SCRIPT_PATH
SHELLS = {
    "bash": {
        "configs": ["~/.bashrc", "~/.bash_profile"],
        "snippet": 'eval "$(register-python-argcomplete {name})"',
    },
    "zsh": {
        "configs": ["~/.zshrc", "~/.zprofile"],
        "snippet": 'eval "$(register-python-argcomplete {name})"',
    },
    "tcsh": {
        "configs": ["~/.tcshrc", "~/.cshrc"],
        "snippet": "eval `register-python-argcomplete --shell tcsh {name}`",
    },
    "fish": {
        "configs": ["~/.config/fish/config.fish"],
        "snippet": None,  # fish uses a separate file, handled specially
        "completions_dir": "~/.config/fish/completions",
        "completions_file": "{name}.fish",
    },
    "pwsh": {
        "configs": [],   # PowerShell profile detected at runtime
        "snippet": 'register-python-argcomplete --shell powershell {name} | Invoke-Expression',
    },
}

MARKER = f"# cqscout argcomplete"


def detect_shells() -> list[str]:
    """Return names of shells whose binaries exist on this system."""
    found = []
    for shell in SHELLS:
        binary = "pwsh" if shell == "pwsh" else shell
        if shutil.which(binary):
            found.append(shell)
    return found


def powershell_profile() -> Path | None:
    """Ask PowerShell where its profile lives."""
    try:
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", "echo $PROFILE"],
            capture_output=True, text=True, timeout=5,
        )
        path = result.stdout.strip()
        return Path(path) if path else None
    except Exception:
        return None


def find_config(shell: str) -> Path | None:
    """Return the first existing config file for the shell, or the preferred one if none exist."""
    configs = SHELLS[shell]["configs"]
    if not configs:
        return None
    for c in configs:
        p = Path(c).expanduser()
        if p.exists():
            return p
    # None exist yet — return the preferred (first) one to be created
    return Path(configs[0]).expanduser()


def already_installed(config: Path, marker: str) -> bool:
    try:
        return marker in config.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False


def append_snippet(config: Path, snippet: str, marker: str) -> None:
    config.parent.mkdir(parents=True, exist_ok=True)
    with config.open("a", encoding="utf-8") as f:
        f.write(f"\n{marker}\n{snippet}\n")


def install_fish() -> bool:
    fish_cfg = SHELLS["fish"]
    completions_dir = Path(fish_cfg["completions_dir"]).expanduser()
    completions_file = completions_dir / fish_cfg["completions_file"].format(name=SCRIPT_NAME)

    if completions_file.exists():
        print(f"  fish: already installed ({completions_file})")
        return True

    try:
        result = subprocess.run(
            ["register-python-argcomplete", "--shell", "fish", SCRIPT_NAME],
            capture_output=True, text=True, check=True,
        )
        completions_dir.mkdir(parents=True, exist_ok=True)
        completions_file.write_text(result.stdout, encoding="utf-8")
        print(f"  fish: installed → {completions_file}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"  fish: failed — {e.stderr.strip()}", file=sys.stderr)
        return False


def main():
    print(f"cqscout autocomplete installer")
    print(f"Script: {SCRIPT_PATH}\n")

    if not SCRIPT_PATH.exists():
        print(f"Error: {SCRIPT_PATH} not found", file=sys.stderr)
        sys.exit(1)

    shells = detect_shells()
    if not shells:
        print("No supported shells detected.", file=sys.stderr)
        sys.exit(1)

    print(f"Detected shells: {', '.join(shells)}\n")

    needs_reload = []

    for shell in shells:
        if shell == "fish":
            install_fish()
            continue

        # PowerShell: ask pwsh for its profile path
        if shell == "pwsh":
            config = powershell_profile()
            if not config:
                print(f"  pwsh: could not determine profile path", file=sys.stderr)
                continue
        else:
            config = find_config(shell)
            if not config:
                print(f"  {shell}: no config file found, skipping")
                continue

        snippet = SHELLS[shell]["snippet"].format(name=SCRIPT_NAME, path=SCRIPT_PATH)
        marker = MARKER

        if already_installed(config, marker):
            print(f"  {shell}: already installed ({config})")
            continue

        append_snippet(config, snippet, marker)
        print(f"  {shell}: installed → {config}")
        needs_reload.append((shell, config))

    if needs_reload:
        print("\nTo activate in your current session:")
        for shell, config in needs_reload:
            print(f"  {shell}:  source {config}")


if __name__ == "__main__":
    main()
