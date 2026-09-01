"""
UiPath XAML Scanner - Invocations & Cross-File Resolution

Split out of scanner.py: everything about how one XAML file links to another via
InvokeWorkflowFile - extracting the raw invocation calls, resolving each
WorkflowFileName to an actual project-relative path, and (once every file has been
scanned) inverting that into a reverse "invoked_by" index.

Unlike graph_flow.py, this module has NO dependency on FlowCtx, the scope stack, or
_process_activity_node - it never needs to resolve a variable reference, only to read
plain XML attributes (WorkflowFileName, argument bindings) and reason about file paths.
The one exception is _resolve_invoke_targets_in_flow, which walks the *already-built*
activity-flow tree (plain dicts, produced earlier by scanner.py's per-activity
processors) to fill in target_relative_path on every InvokeWorkflowFile node it finds -
it needs the tree's shape, not FlowCtx itself.

Public interface (called from scanner.py's parse_xaml_file / build_catalog):
  - extract_argument_bindings(invoke_elem) -> list[dict] (called per-node from
    scanner.py's _process_activity_node now, not just from extract_invocations)
  - collect_invocations_in_flow_order(activity_flow) -> list[dict] (the current source
    of a file's "invokes" list - see its own docstring for why)
  - resolve_invocation_target(xaml_path, raw_path, project_root, known_paths, by_filename) -> str | None
  - extract_namespaces(xaml_path) -> dict[str, str]
  - resolve_invoke_targets_in_flow(steps, xaml_path, project_root, known_paths, by_filename) -> None
  - build_dependency_index(entries) -> None
  - find_main_entry(entries) -> dict | None
  - get_unreachable_paths(entries) -> set[str]

extract_invocations(root) -> list[dict] still exists below but is no longer called from
scanner.py. It walked the raw XML tree via root.iter(), which only matches Studio's
visual order for tree-shaped roots (Sequence/If/Switch/TryCatch) - not graph-shaped
ones (Flowchart/StateMachine) - see collect_invocations_in_flow_order's docstring.
Kept in case it's useful elsewhere (e.g. a quick invoke count without a full
activity-flow walk); just don't use it as the source of a displayed invokes order.

Dependency direction: this module imports shared namespace constants and local_name from
scanner.py. scanner.py does NOT import this module at the top level - it imports these
five functions lazily, inside parse_xaml_file and build_catalog, to avoid a circular
import (scanner.py <-> invocations.py), the same pattern used for graph_flow.py.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

from scanner import (
    X_NS,
    UI_NS,
    INVOKE_WORKFLOW_TAG,
    _ARGUMENT_DIRECTION_PREFIXES,
    local_name,
)


def extract_argument_bindings(invoke_elem: ET.Element) -> list[dict]:
    """
    Extracts expressions passed to each argument in an InvokeWorkflowFile call
    from <ui:InvokeWorkflowFile.Arguments>. Shows actual data flow:
    "A passes expression X to B's argument Y".
    """
    key_attr = f"{{{X_NS}}}Key"
    type_args_attr = f"{{{X_NS}}}TypeArguments"

    container = invoke_elem.find(f"{{{UI_NS}}}InvokeWorkflowFile.Arguments")
    if container is None:
        return []

    bindings = []
    for child in container.iter():
        if child is container:
            continue
        tag_name = local_name(child.tag)  # InArgument / OutArgument / InOutArgument
        if tag_name not in _ARGUMENT_DIRECTION_PREFIXES:
            # Not an actual argument binding - e.g. a wrapper element like
            # <scg:Dictionary x:TypeArguments="x:String, Argument"> that XAML uses to
            # represent zero arguments (empty) or to hold real bindings as descendants
            # rather than direct children. Skip it either way; its real InArgument/
            # OutArgument/InOutArgument descendants (if any) are still visited by iter().
            continue
        direction = _ARGUMENT_DIRECTION_PREFIXES[tag_name]
        bindings.append(
            {
                "argument_name": child.get(key_attr, ""),
                "direction": direction,
                "type": child.get(type_args_attr, ""),
                "expression": (child.text or "").strip(),
            }
        )
    return bindings


def extract_invocations(root: ET.Element) -> list[dict]:
    """Extracts all InvokeWorkflowFile calls in the file."""
    invocations = []
    for elem in root.iter(INVOKE_WORKFLOW_TAG):
        raw_path = elem.get("WorkflowFileName", "")
        is_dynamic = raw_path.strip().startswith("[") or not raw_path
        invocations.append(
            {
                "display_name": elem.get("DisplayName", ""),
                "raw_path": raw_path,
                "is_dynamic": is_dynamic,
                "argument_bindings": extract_argument_bindings(elem),
            }
        )
    return invocations


def resolve_invocation_target(
    xaml_path: Path,
    raw_path: str,
    project_root: Path,
    known_paths: set[str],
    by_filename: dict[str, list[str]],
) -> str | None:
    """
    Resolves a WorkflowFileName (e.g. 'Framework\\InitAllSettings.xaml') to a project-relative
    path. UiPath always resolves WorkflowFileName relative to the *invoking* file's own folder,
    so that's tried first. But workflows that live inside a Framework/-style subfolder often
    invoke business workflows using just a bare filename (no folder prefix) even though those
    files actually live elsewhere in the project (e.g. the project root) - the naive
    same-directory guess then points at a path nothing was scanned at. When that happens, fall
    back to a project-wide search by filename so the invocation still links up correctly.
    """
    normalized = raw_path.replace("\\", "/")

    try:
        resolved = (xaml_path.parent / normalized).resolve()
        candidate = resolved.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        candidate = None

    if candidate is not None and candidate in known_paths:
        return candidate

    filename = normalized.rsplit("/", 1)[-1]
    matches = by_filename.get(filename, [])
    if len(matches) == 1:
        return matches[0]

    # Either no file anywhere in the project has this name, or more than one does (ambiguous -
    # picking one would risk silently wiring the tree to the wrong file). In both cases return
    # None so this invocation surfaces in 'unresolved_invocations' for manual review instead of
    # pointing at a path that doesn't correspond to any real scanned file.
    return None


def extract_namespaces(xaml_path: Path) -> dict[str, str]:
    """Extracts namespace mappings for the file (xmlns:x, xmlns:sap, etc.)."""
    namespaces = {}
    for event, (prefix, uri) in ET.iterparse(xaml_path, events=["start-ns"]):
        namespaces[prefix] = uri
    return namespaces


def resolve_invoke_targets_in_flow(
    steps: list[dict],
    xaml_path: Path,
    project_root: Path,
    known_paths: set[str],
    by_filename: dict[str, list[str]],
) -> None:
    """
    Walks the activity flow tree in-place and resolves 'target_relative_path' for every
    InvokeWorkflowFile node found anywhere in it.
    """
    for step in steps:
        if step["type"] == "activity":
            if step["tag"] == "InvokeWorkflowFile":
                if step.get("is_dynamic"):
                    step["target_relative_path"] = None
                else:
                    step["target_relative_path"] = resolve_invocation_target(
                        xaml_path, step.get("raw_path", ""), project_root, known_paths, by_filename
                    )
            resolve_invoke_targets_in_flow(
                step.get("children") or [], xaml_path, project_root, known_paths, by_filename
            )
            # A State node's outgoing transitions can each carry their own embedded
            # "action" activity (Transition.Action) - not part of "children" above, since
            # it belongs to the edge rather than the state itself - so it needs its own walk.
            for transition in step.get("transitions") or []:
                action = transition.get("action")
                if action is not None:
                    resolve_invoke_targets_in_flow(
                        [action], xaml_path, project_root, known_paths, by_filename
                    )
        else:
            for branch in step["branches"]:
                resolve_invoke_targets_in_flow(
                    branch.get("steps") or [], xaml_path, project_root, known_paths, by_filename
                )


def collect_invocations_in_flow_order(steps: list[dict]) -> list[dict]:
    """
    Walks activity_flow (call AFTER resolve_invoke_targets_in_flow has filled in
    target_relative_path on it) and returns one dict per InvokeWorkflowFile node
    encountered, in the exact order the flow tree visits them - i.e. the same
    visual/logical order a developer reading the file in Studio would see, not raw XML
    document order.

    This replaces extract_invocations's root.iter(INVOKE_WORKFLOW_TAG) approach as the
    source of a file's "invokes" list (scanner.py's parse_xaml_file). That approach
    walked the raw XML tree, which only matches Studio's visual order for tree-shaped
    roots (Sequence/If/Switch/TryCatch) - for a graph-shaped root (Flowchart/
    StateMachine) tree/document order can diverge arbitrarily from the order the graph
    is actually walked. Concretely: in a REFramework Main.xaml, the StateMachine's
    "Initialization" state is written LAST in the raw XML (it's the state most other
    states transition back to, so under .NET XAML's object-graph-sharing form its only
    full inline occurrence ends up wherever it first happens to be referenced - not
    necessarily first) even though it's the state that actually runs FIRST - see
    graph_flow.py's module docstring and _walk_state_machine's docstring. activity_flow
    is already built in correct graph-walk order for those roots (that's what fixed the
    equivalent per-file activity-list ordering problem), so deriving "invokes" from it
    - rather than from a second, independent raw-XML pass - inherits that correctness
    for free, and keeps the two views (the per-file activity list and the invokes list,
    and in turn the project map, which is built from invokes) consistent by
    construction instead of by coincidence.

    Mirrors resolve_invoke_targets_in_flow's traversal shape exactly (activity/
    children, a State node's transitions[].action, branch/branches) so the two walks
    visit nodes in the same order - that function must still be called first, since
    this one only reads target_relative_path/argument_bindings/raw_path/is_dynamic
    rather than computing any of them.
    """
    found: list[dict] = []
    for step in steps:
        if step["type"] == "activity":
            if step["tag"] == "InvokeWorkflowFile":
                found.append(
                    {
                        "display_name": step.get("name", ""),
                        "raw_path": step.get("raw_path", ""),
                        "is_dynamic": step.get("is_dynamic", False),
                        "argument_bindings": step.get("argument_bindings", []),
                        "target_relative_path": step.get("target_relative_path"),
                    }
                )
            found.extend(collect_invocations_in_flow_order(step.get("children") or []))
            # Mirrors resolve_invoke_targets_in_flow: a State node's outgoing transitions
            # can each carry their own embedded action activity (Transition.Action),
            # which isn't part of "children" - it belongs to the edge, not the state.
            for transition in step.get("transitions") or []:
                action = transition.get("action")
                if action is not None:
                    found.extend(collect_invocations_in_flow_order([action]))
        else:
            for branch in step["branches"]:
                found.extend(collect_invocations_in_flow_order(branch.get("steps") or []))
    return found


def build_dependency_index(entries: list[dict]) -> None:
    """Adds an 'invoked_by' list to each entry (inverting 'invokes'). Operates in-place."""
    by_path = {e["relative_path"]: e for e in entries if e.get("parse_ok")}

    for entry in by_path.values():
        entry["invoked_by"] = []

    for entry in entries:
        if not entry.get("parse_ok"):
            continue
        for inv in entry.get("invokes", []):
            target = inv.get("target_relative_path")
            if target and target in by_path:
                by_path[target]["invoked_by"].append(entry["relative_path"])


MAIN_FILENAME = "Main.xaml"


def find_main_entry(entries: list[dict]) -> dict | None:
    """
    Finds the project's Main.xaml entry - the root every other file is expected to
    descend from via InvokeWorkflowFile. By convention it always lives directly in the
    project root, i.e. its relative_path (POSIX-style, as produced by parse_xaml_file)
    has no '/' in it. Returns None if no such file was found (or it failed to parse),
    in which case reachability can't be computed and every file should be kept as-is
    (see get_unreachable_paths).
    """
    for entry in entries:
        if entry.get("parse_ok") and entry["relative_path"] == MAIN_FILENAME:
            return entry
    return None


def get_unreachable_paths(entries: list[dict]) -> set[str]:
    """
    Returns the relative_path of every successfully-parsed file that is NOT reachable
    from Main.xaml by following InvokeWorkflowFile calls (directly or transitively).
    Must be called after build_dependency_index, since it walks 'invokes'/
    'target_relative_path' the same way that function does.

    Files that fail to parse are never included here (nothing can safely follow from
    them either way), and if Main.xaml itself can't be found, returns an empty set -
    treat that as "reachability unknown" rather than "everything is unreachable".
    """
    by_path = {e["relative_path"]: e for e in entries if e.get("parse_ok")}

    main_entry = find_main_entry(entries)
    if main_entry is None:
        return set()

    visited: set[str] = set()
    stack = [main_entry["relative_path"]]
    while stack:
        path = stack.pop()
        if path in visited:
            continue
        visited.add(path)
        entry = by_path.get(path)
        if entry is None:
            continue
        for inv in entry.get("invokes", []):
            target = inv.get("target_relative_path")
            if target and target not in visited:
                stack.append(target)

    return set(by_path) - visited