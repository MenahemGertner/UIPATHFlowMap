import base64
import json
import os
import tempfile
import threading
import webbrowser
from pathlib import Path
from tkinter import Tk, messagebox, ttk

from ai_export import build_all_exports, build_all_file_exports
from extra_sources import build_extra_sources
from project_io import get_output_dir
from prompts import PROMPTS

# --- Default View Filter ---
# Filtering logic for the default UI view:
# - NOISE_ACTIVITY_TAGS: Single activities that don't contribute to structural/logic understanding are removed.
# - NOISE_CONTAINER_TAGS: Operational wrappers removed, but their children are promoted to the parent level.

NOISE_ACTIVITY_TAGS = {"LogMessage", "CommentOut", "WriteLine", "Delay", "Comment"}
NOISE_CONTAINER_TAGS = {"RetryScope"}
_SCRIPT_DIR = Path(__file__).resolve().parent
HTML_REPORT_TEMPLATE = (_SCRIPT_DIR / "project_map.html").read_text(encoding="utf-8")
VIS_NETWORK_JS = (_SCRIPT_DIR / "vis-network.min.js").read_text(encoding="utf-8")
AI_PANEL_HTML = (_SCRIPT_DIR / "ai_panel.html").read_text(encoding="utf-8")
EDGE_TOOLTIP_HTML = (_SCRIPT_DIR / "edge_tooltip_fragment.html").read_text(encoding="utf-8")
DRAWER_HTML = (_SCRIPT_DIR / "drawer_fragment.html").read_text(encoding="utf-8")

def _filter_steps(steps: list[dict] | None) -> list[dict]:
    if not steps:
        return []

    filtered: list[dict] = []
    for step in steps:
        if step["type"] == "activity":
            if step["tag"] in NOISE_ACTIVITY_TAGS:
                continue
            if step["tag"] in NOISE_CONTAINER_TAGS:
                filtered.extend(_filter_steps(step.get("children")))
                continue

            new_children = _filter_steps(step.get("children"))

            if step["tag"] == "Sequence" and not new_children:
                continue

            new_step = dict(step)
            new_step["children"] = new_children
            filtered.append(new_step)
        else:  # branch: if / switch / trycatch / while (DoWhile too - same "kind": "while")
            if step["kind"] == "trycatch":
                try_branch = next((b for b in step["branches"] if b["label"] == "Try"), None)
                if try_branch is not None:
                    filtered.extend(_filter_steps(try_branch.get("steps")))
                continue

            new_branches = []
            for branch in step["branches"]:
                branch_steps = _filter_steps(branch.get("steps"))
                if not branch_steps:
                    continue
                new_branches.append({**branch, "steps": branch_steps})

            if not new_branches:
                # Every branch's content was filtered out as noise (e.g. branches that
                # only log). Unlike a Sequence - a passive wrapper that's dropped when
                # empty because it has no content of its own - an If/Switch/While's
                # condition IS content: exactly the "structural/logic understanding" the
                # default view exists to surface. So the decision (or loop) stays, just
                # with empty branch bodies, instead of vanishing entirely.
                new_branches = [{**b, "steps": []} for b in step["branches"]]

            filtered.append({**step, "branches": new_branches})
    return filtered


def add_default_display_flow(catalog: dict) -> dict:
    """
    Adds 'activity_flow_display' to each file in the catalog (filtered view for default UI).
    The original full 'activity_flow' remains untouched for future use (e.g., full view toggle).
    """
    for f in catalog["files"]:
        if f.get("parse_ok"):
            f["activity_flow_display"] = _filter_steps(f["activity_flow"])
    return catalog


def show_message(kind: str, title: str, message: str) -> None:
    """
    Displays a graphical message box instead of printing to the terminal.
    Allows running without a console window.
    """
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()

    if kind == "info":
        messagebox.showinfo(title, message, parent=root)
    elif kind == "warning":
        messagebox.showwarning(title, message, parent=root)
    elif kind == "error":
        messagebox.showerror(title, message, parent=root)

    root.destroy()


def run_with_progress(title: str, message: str, work_fn):
    """
    Shows a small indeterminate-progress window while `work_fn` (a zero-argument
    callable, e.g. a lambda wrapping build_catalog(project_root)) runs on a
    background thread, then closes the window and returns work_fn's result.

    Tkinter widgets may only be created/updated from the main thread, so the
    scan itself can't just run inline with a progress bar ticking on top of it -
    the bar would freeze the instant the main thread blocks on build_catalog.
    Instead the scan runs on a worker thread while the main thread stays free to
    drive the window's event loop (root.update, the indeterminate bar's own
    internal animation), then the two are rejoined once the worker finishes.

    Exceptions raised inside work_fn are captured (not re-raised on the worker
    thread, where they'd be invisible - Tkinter mainloops don't propagate
    worker-thread exceptions) and re-raised here on the main thread instead, so
    the existing try/except Exception in main.py's main() still catches them
    and shows the usual error messagebox.
    """
    root = Tk()
    root.title(title)
    root.resizable(False, False)
    root.attributes("-topmost", True)

    width, height = 320, 110
    root.update_idletasks()
    x = (root.winfo_screenwidth() - width) // 2
    y = (root.winfo_screenheight() - height) // 2
    root.geometry(f"{width}x{height}+{x}+{y}")

    label = ttk.Label(root, text=message, padding=(16, 20, 16, 8))
    label.pack()
    bar = ttk.Progressbar(root, mode="indeterminate", length=260)
    bar.pack(padx=16, pady=(0, 16))
    bar.start(12)

    result: dict = {}

    def worker():
        try:
            result["value"] = work_fn()
        except Exception as e:  # noqa: BLE001 - deliberately broad, re-raised on main thread below
            result["error"] = e
        finally:
            root.after(0, root.destroy)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    root.mainloop()
    thread.join()

    if "error" in result:
        raise result["error"]
    return result["value"]


def _build_ai_panel_data(catalog: dict, project_root: Path) -> tuple[str, str, str]:
    """
    Builds the three JSON payloads the AI panel needs (see ai_panel.html): the
    compact per-purpose whole-project exports (ai_export.build_all_exports), the
    per-file exports for the drawer's "Ask AI about this file" button
    (ai_export.build_all_file_exports), and the instruction prompts shared by both
    scopes (prompts.py - the "file_deep_dive" key lives in the same PROMPTS dict as
    "debug"/"business_doc", see prompts.py's PROMPTS registration, so a single
    payload covers both). Runs extra_sources.py fresh here rather than accepting it
    as a parameter, since it's cheap (a JSON read + an Excel read) and keeping it
    out of main.py's call signature means this function alone owns "what AI export
    needs beyond the XAML catalog" - main.py doesn't need to know that detail
    changed if a third source is added later. build_all_file_exports doesn't touch
    extras at all (see its docstring in ai_export.py), so it's called independently
    of the `extras` built for the whole-project exports.

    Returns (ai_exports_json, ai_file_exports_json, ai_prompts_json) as JSON-encoded
    strings, ready to be dropped verbatim into the text/plain elements
    project_map.html reads via JSON.parse - see the AI_EXPORTS / AI_FILE_EXPORTS /
    AI_PROMPTS loading comment there.
    """
    extras = build_extra_sources(project_root)
    exports = build_all_exports(catalog, extras)
    file_exports = build_all_file_exports(catalog)
    return (
        json.dumps(exports, ensure_ascii=False),
        json.dumps(file_exports, ensure_ascii=False),
        json.dumps(PROMPTS, ensure_ascii=False),
    )


def open_project_map(catalog: dict, project_root: Path, debug: bool = False) -> Path:
    """
    Generates the standalone HTML report with embedded catalog data and template source,
    both embedded as base64 rather than raw JSON strings. This sidesteps a subtle HTML
    parsing hazard: <script> tags (even type="application/json") are parsed by the browser
    using "raw text" rules that look for the literal sequence "</script" anywhere in the
    content, regardless of quoting/escaping - and the raw template necessarily contains real
    </script> tags. Base64's alphabet cannot contain that sequence, so this class of bug is
    structurally impossible rather than merely escaped.

    The AI panel (ai_panel.html) is injected the same way as vis-network.min.js - a
    plain-text placeholder swap, since it's HTML/CSS/JS that needs to render as
    actual markup, not data. Its three data payloads (AI_EXPORTS, AI_FILE_EXPORTS,
    AI_PROMPTS) go through the JSON.parse(textContent) path instead (see
    project_map.html) - AI_FILE_EXPORTS backs the drawer's "Ask AI about this file"
    button (scope 'file' in ai_panel.html's openAiModal), keyed by relative_path
    rather than by purpose the way AI_EXPORTS is. The panel's own HTML/CSS/JS is
    separately base64-stashed under __AI_PANEL_B64_PLACEHOLDER__'s sibling element
    purely so the in-browser "Save As" flow can regenerate a fully working
    standalone copy without contacting Python again - see saveMapAsHtml's comments.

    The edge-tooltip fragment (edge_tooltip_fragment.html) - the CSS/markup/JS for
    the invocation-argument tooltip, previously inline in project_map.html - follows
    the identical two-placeholder pattern as the AI panel, for the identical reason:
    the plain-text placeholder swap injects it as live markup at generation time, and
    the base64 sibling lets saveMapAsHtml carry it forward when the map is re-saved
    from the browser without another Python run.

    The drawer fragment (drawer_fragment.html) - the CSS/markup/JS for the side
    panel that shows a selected file's activity flow, variables, and invoke
    relationships, previously inline in project_map.html - follows the same
    two-placeholder pattern as the other two fragments, for the same reason.

    Where the generated HTML actually lands depends on `debug`. The tool runs on
    client servers, so the shipped default (debug=False) must never write into the
    scanned project itself - the report is written to a private OS temp file
    instead, which is enough for webbrowser.open() (it needs *some* file:// URI)
    without leaving anything behind in project_root. debug=True is strictly a local
    development aid: it restores the old behavior of writing into
    project_root/.xaml_scan_output (via project_io.get_output_dir) so the file can
    be inspected/diffed across runs. Either way, the in-browser "Save As" button
    (saveMapAsHtml in project_map.html) is unaffected - it rebuilds the HTML
    entirely client-side from the payloads already embedded in the page, and never
    reads this function's output back off disk.
    """
    catalog_with_display = add_default_display_flow(catalog)
    catalog_json = json.dumps(catalog_with_display, ensure_ascii=False)
    catalog_b64 = base64.b64encode(catalog_json.encode("utf-8")).decode("ascii")

    raw_template_b64 = base64.b64encode(HTML_REPORT_TEMPLATE.encode("utf-8")).decode("ascii")
    ai_panel_b64 = base64.b64encode(AI_PANEL_HTML.encode("utf-8")).decode("ascii")
    edge_tooltip_b64 = base64.b64encode(EDGE_TOOLTIP_HTML.encode("utf-8")).decode("ascii")
    drawer_b64 = base64.b64encode(DRAWER_HTML.encode("utf-8")).decode("ascii")

    ai_exports_json, ai_file_exports_json, ai_prompts_json = _build_ai_panel_data(catalog, project_root)

    html_content = (
        HTML_REPORT_TEMPLATE
        .replace("__CATALOG_JSON_B64_PLACEHOLDER__", catalog_b64)
        .replace("__VIS_NETWORK_JS_PLACEHOLDER__", VIS_NETWORK_JS)
        .replace("__RAW_TEMPLATE_B64_PLACEHOLDER__", raw_template_b64)
        .replace("__AI_PANEL_PLACEHOLDER__", AI_PANEL_HTML)
        .replace("__AI_PANEL_B64_PLACEHOLDER__", ai_panel_b64)
        .replace("__AI_EXPORTS_JSON_PLACEHOLDER__", ai_exports_json)
        .replace("__AI_FILE_EXPORTS_JSON_PLACEHOLDER__", ai_file_exports_json)
        .replace("__AI_PROMPTS_JSON_PLACEHOLDER__", ai_prompts_json)
        .replace("__EDGE_TOOLTIP_PLACEHOLDER__", EDGE_TOOLTIP_HTML)
        .replace("__EDGE_TOOLTIP_B64_PLACEHOLDER__", edge_tooltip_b64)
        .replace("__DRAWER_PLACEHOLDER__", DRAWER_HTML)
        .replace("__DRAWER_B64_PLACEHOLDER__", drawer_b64)
    )

    if debug:
        output_dir = get_output_dir(project_root)
        output_path = output_dir / "_project_map.html"
        output_path.write_text(html_content, encoding="utf-8")
    else:
        fd, tmp_path = tempfile.mkstemp(suffix=".html", prefix="flowmap_")
        os.close(fd)
        output_path = Path(tmp_path)
        output_path.write_text(html_content, encoding="utf-8")

    webbrowser.open(output_path.resolve().as_uri())
    return output_path