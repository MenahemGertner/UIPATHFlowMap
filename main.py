import json
import traceback
from pathlib import Path

from scanner import build_catalog, pick_project_folder
from project_io import get_output_dir
from report import show_message, open_project_map, run_with_progress

# When False (the shipped default), the tool never writes any file into the
# scanned project - the HTML report is generated in memory and opened straight
# from a temp file, and no JSON catalog is written at all. Flip to True only
# for local debugging: this restores the old behavior of dumping both
# artifacts into project_root/.xaml_scan_output for inspection.
DEBUG_WRITE_FILES = False


def main():
    try:
        project_root = pick_project_folder()

        if project_root is None:
            show_message("info", "Cancelled", "No folder selected. Exiting tool.")
            return

        if not (project_root / "project.json").exists():
            show_message(
                "warning",
                "Warning",
                f"project.json not found in:\n{project_root}\n\n"
                "Please make sure you selected the root directory of a UiPath project.\n"
                "Scanning will proceed anyway.",
            )

        catalog = run_with_progress(
            "FlowMap",
            "Scanning project...",
            lambda: build_catalog(project_root),
        )

        if DEBUG_WRITE_FILES:
            output_dir = get_output_dir(project_root)
            output_path = output_dir / "_xaml_catalog.json"
            output_path.write_text(
                json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        # Generate HTML report and open directly in browser. debug=True keeps the
        # old behavior (written into project_root/.xaml_scan_output); debug=False
        # opens it straight from a temp file, leaving nothing behind in the project.
        open_project_map(catalog, project_root, debug=DEBUG_WRITE_FILES)

    except Exception:
        # Prevents silent crashes when running without a terminal window
        show_message(
            "error",
            "Unexpected Error",
            "An error occurred during scan:\n\n" + traceback.format_exc(),
        )


if __name__ == "__main__":
    main()