"""
Project I/O - Non-logic helpers for the UiPath XAML Scanner

Everything here is plumbing around the scan, not part of the extraction logic itself:
- Prompting the user to pick a project folder (a tkinter dialog).
- Locating the .xaml files to scan within that folder.
- Providing a single, dedicated output folder for everything the scan produces (the JSON
  catalog, the generated HTML report, and any future output), so nothing the tool writes
  ends up scattered loose among the project's own XAML/project.json files.

Kept separate from scanner.py so that file can stay focused purely on parsing/extraction
logic, with nothing in it that isn't either core logic or directly in service of it.
"""

from pathlib import Path
from tkinter import Tk, filedialog

# Every file this tool generates (catalog JSON, HTML report, anything added later) goes
# under this subfolder of the scanned project, instead of loose in the project root next
# to the developer's own XAML/project.json files. Centralized here as the single place
# that name is defined, so every caller (current and future) stays in sync automatically.
OUTPUT_DIR_NAME = ".xaml_scan_output"


def get_output_dir(project_root: Path) -> Path:
    """
    Returns the dedicated output folder for a project's scan results, creating it (and any
    missing parent folders) if it doesn't exist yet. Callers that write scan output (the
    JSON catalog, the HTML report, etc.) should build their file paths from this instead of
    from project_root directly.
    """
    output_dir = project_root / OUTPUT_DIR_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def pick_project_folder() -> Path | None:
    """Opens a directory picker dialog. Returns None if cancelled."""
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    root.lift()
    root.focus_force()

    folder = filedialog.askdirectory(
        title="Select UiPath Project Directory (containing project.json)",
        parent=root,
    )
    root.destroy()

    return Path(folder) if folder else None


def find_xaml_files(project_root: Path) -> list[Path]:
    """Recursively finds and returns all .xaml files in the project."""
    return sorted(project_root.rglob("*.xaml"))