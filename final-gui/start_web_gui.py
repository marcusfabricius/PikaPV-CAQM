#!/usr/bin/env python3
from __future__ import annotations
import importlib
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SRC_DIR = BASE_DIR / "src"
REQUIREMENTS_FILE = SRC_DIR / "requirements.txt"
ENTRYPOINTS = ["web_gui.py", "web_app.py"]
COMMON_IMPORT_NAMES = {
    "flask": "flask",
    "pyvisa": "pyvisa",
    "matplotlib": "matplotlib",
}


def normalize_requirement_name(requirement: str) -> str:
    requirement = requirement.strip()
    if not requirement or requirement.startswith("#"):
        return ""
    for sep in ["==", ">=", "<=", "~=", "!=", ">", "<"]:
        if sep in requirement:
            requirement = requirement.split(sep, 1)[0]
            break
    return requirement.strip().lower()


def import_name_for_requirement(requirement_name: str) -> str:
    return COMMON_IMPORT_NAMES.get(requirement_name, requirement_name)


def check_imports(requirements_file: Path) -> list[str]:
    missing = []
    if not requirements_file.exists():
        raise FileNotFoundError(f"Requirements file not found: {requirements_file}")
    with requirements_file.open("r", encoding="utf-8") as handle:
        for line in handle:
            req_name = normalize_requirement_name(line)
            if not req_name:
                continue
            import_name = import_name_for_requirement(req_name)
            try:
                importlib.import_module(import_name)
            except Exception:
                missing.append(req_name)
    return missing


def install_requirements(requirements_file: Path) -> bool:
    python_executable = sys.executable
    install_command = [python_executable, "-m", "pip", "install", "-r", str(requirements_file)]
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        install_command.insert(3, "--user")
    print("Installing required Python packages...")
    result = subprocess.run(install_command)
    return result.returncode == 0


def find_entrypoint() -> Path:
    for entrypoint in ENTRYPOINTS:
        for candidate in (BASE_DIR / entrypoint, SRC_DIR / entrypoint):
            if candidate.exists():
                return candidate
    raise FileNotFoundError(
        "No web entrypoint found. Expected web_gui.py or web_app.py in the main folder or src/."
    )


def main() -> int:
    print("Starting PikaPV GUI launcher...")
    try:
        missing = check_imports(REQUIREMENTS_FILE)
    except FileNotFoundError as error:
        print(error)
        return 1

    if missing:
        print("Missing Python packages:", ", ".join(missing))
        if not install_requirements(REQUIREMENTS_FILE):
            print("Failed to install required packages. Please install them manually:")
            print(f"  {sys.executable} -m pip install -r {REQUIREMENTS_FILE}")
            return 1
        missing = check_imports(REQUIREMENTS_FILE)
        if missing:
            print("Still missing packages after install:", ", ".join(missing))
            return 1
    else:
        print("All required Python packages are already installed.")

    entrypoint = find_entrypoint()
    print(f"Running {entrypoint.name}...")

    command = [sys.executable, str(entrypoint)] + sys.argv[1:]
    exit_code = subprocess.call(command)
    if exit_code != 0:
        print(f"{entrypoint.name} exited with status {exit_code}.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
