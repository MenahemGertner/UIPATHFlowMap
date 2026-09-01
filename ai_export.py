"""
UiPath AI Export - compact, purpose-aware formatting for AI consumption.

Takes the scanner.py catalog (rich, built for the interactive project map) and the
extra_sources.py result (project.json + Config.xlsx), and produces a compact text
block ready to paste into an AI chat.

Two things happen here, kept deliberately separate:

1. TRIMMING (purpose-independent): strip fields that exist only for the interactive
   UI and carry no semantic value for an AI reading the project - namespaces
   (near-identical XAML boilerplate across every file), the variable_declarations
   index (a lookup table duplicating what's already inline on every reference as
   decl_id - useful for click-to-jump in the UI, redundant for a linear read), and
   the assignments/arguments duplication on Assign nodes (same To/Value pair
   represented twice in two shapes).

2. SHAPING (purpose-dependent): once trimmed, a purpose decides what additional
   detail is worth keeping or dropping - e.g. "debug" (a project-wide architecture
   review) keeps LogMessage/RetryScope/TryCatch activities because judging whether
   error-handling and retry patterns are applied *consistently* across files is
   exactly the point of that purpose; "business_doc" drops them as operational noise
   and leans on annotations instead. See PURPOSES below.

A third, separate concern lives here too:

3. RENDERING: once a file's entry is trimmed and shaped, it still has to be written
   out as text. The straightforward option - json.dumps(shaped, indent=1) - pays
   JSON's per-node syntax tax on every activity node: the keys "type", "tag",
   "name", "children", every brace/bracket/comma/quote. None of that punctuation is
   information the AI needs - it's the price of a format built for machines to
   parse, not for a model to read. render_file_entry() below carries the exact same
   surviving information (control flow shape, activity names/tags, conditions,
   assignments, arguments, annotations) as an indented ASCII tree instead, at
   roughly a third of the character count on real projects. It changes nothing
   about WHAT survives steps 1-2 - only how the surviving dict is written.

This module has no knowledge of prompts or UI - it returns plain text blocks. The
prompt templates and the "which purposes exist" UI wiring belong to a later layer.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Step 1: purpose-independent trimming of a single file's catalog entry
# ---------------------------------------------------------------------------

# Tags whose entire purpose is operational noise for a *reading* AI, not business or
# control-flow logic. Filtered out by _strip_activity_tree when a purpose's
# `drop_tags` includes them - kept as a shared constant so purposes compose from the
# same vocabulary instead of redefining their own tag lists.
NOISE_TAGS = {"LogMessage", "RetryScope", "TryCatch", "Delay", "CommentOut"}

# Of the tags above, RetryScope is the wrapper-is-noise-but-contents-aren't case: its
# NumberOfRetries/interval settings aren't relevant to a reading AI, but what it
# retries almost always is - in some files (e.g. "Read data table.xaml",
# "Framework/InitAllApplications.xaml") a single RetryScope wraps the file's *entire*
# body, so dropping it wholesale (wrapper + contents, like every other noise tag)
# left those files looking empty ("Flow: (no activities)") even though they're full
# of real logic. _drop_tags_from_tree below unwraps tags in this set - drops the
# node but splices its children into its place - instead of deleting the subtree.
# CommentOut is deliberately NOT here: its contents are genuinely disabled/dead
# activities (verified: e.g. an "Ignored Activities" sequence with a
# "JUST FOR TEST" assign), so deleting the wrapper *and* its contents is correct.
#
# Note: "TryCatch" in NOISE_TAGS above has never actually matched anything - a
# try/catch in this catalog is represented as a `branch` node with kind="trycatch",
# not an `activity` node with tag="TryCatch" (only activity nodes are tag-filtered
# below), so it's inert. That's accidentally saved us from the same bug this fixes:
# a TryCatch's Try body is usually the file's real logic too, so filtering it the
# same way RetryScope was filtered would have caused the identical failure mode.
UNWRAP_TAGS = {"RetryScope"}


def _trim_file_entry(entry: dict) -> dict:
    """
    Applies the purpose-independent cuts to one file's catalog entry (see module
    docstring, point 1). Returns a new dict; does not mutate the input, since the
    same in-memory catalog from scanner.py may still be needed by report.py for the
    interactive map in the same run.
    """
    if not entry.get("parse_ok"):
        # Failed-to-parse files still matter to an AI (e.g. "why won't this workflow
        # even parse" is itself a debugging question) - pass through unchanged.
        return dict(entry)

    trimmed = {
        "relative_path": entry["relative_path"],
        "arguments": entry.get("arguments", []),
        "variables": entry.get("variables", []),
        "invokes": [_trim_invocation(inv) for inv in entry.get("invokes", [])],
        "invoked_by": entry.get("invoked_by", []),
        "activity_flow": [_trim_activity_node(n) for n in entry.get("activity_flow", [])],
    }
    # namespaces and variable_declarations are intentionally omitted - see module
    # docstring. arguments/variables are kept as-is: they're already compact
    # (name/type/scope, no XML noise) and are the fastest way for an AI to see a
    # file's public contract without reading the whole flow.
    return trimmed


def _trim_invocation(inv: dict) -> dict:
    """Drops display_name (redundant with the file path it resolves to) and is_dynamic
    when False (the common case - only worth flagging when True).

    argument_bindings is intentionally NOT kept here anymore: this feeds the
    file-level "Invokes:" summary line, which is meant as a quick dependency list.
    The real bindings (what's actually passed at each call) are rendered inline in
    the activity_flow tree instead (see _trim_activity_node), right where the call
    happens - the only place they have enough context to be useful (e.g. when the
    same target is invoked twice from different branches with different arguments).
    Keeping both would just double the token cost for the same information.
    """
    result = {
        "raw_path": inv.get("raw_path"),
        "target_relative_path": inv.get("target_relative_path"),
    }
    if inv.get("is_dynamic"):
        result["is_dynamic"] = True
    return result


def _trim_activity_node(node: dict) -> dict:
    """
    Recursively trims one activity_flow node (either an "activity" or "branch" node -
    see scanner.py's _process_activity_node / _process_if_node family).

    The one structural merge that happens here regardless of purpose: Assign/
    MultipleAssign nodes carry both `assignments` (To/Value pairs) and a generic
    `arguments` list that represents the *same* To/Value pair a second time in a
    different shape (see scanner.py's _process_activity_node, where both are built
    from the same underlying elements). `assignments` is the more readable form, so
    `arguments` is dropped whenever `assignments` is already present on the same
    node - not a purpose choice, just deduplication.
    """
    if node.get("type") == "branch":
        trimmed = {
            "type": "branch",
            "kind": node["kind"],
            "condition": node.get("condition") or None,
            "branches": [
                {
                    "label": b["label"],
                    "condition": b.get("condition") or None,
                    "steps": [_trim_activity_node(s) for s in b.get("steps", [])],
                }
                for b in node["branches"]
            ],
        }
        if node.get("annotation"):
            trimmed["annotation"] = node["annotation"]
        # `name` is intentionally skipped: for branch nodes it's things like "Do
        # While" for a `kind: "while"` node - it just restates the kind in words,
        # so it isn't worth its tokens. `key_arguments` (e.g. a Do-While's
        # MaxIterations) and `scope_variable` (e.g. a Catch block's exception
        # variable name) are kept - they're compact and are real configuration a
        # reader can't infer from the condition/kind alone.
        if node.get("key_arguments"):
            trimmed["key_arguments"] = [
                {"name": a["name"], "value": a["value"]} for a in node["key_arguments"]
            ]
        if node.get("scope_variable"):
            trimmed["scope_variable"] = node["scope_variable"]
        return trimmed

    trimmed = {
        "type": "activity",
        "name": node.get("name"),
        "tag": node.get("tag"),
    }
    if node.get("annotation"):
        trimmed["annotation"] = node["annotation"]
    if node.get("scope_variable"):
        trimmed["scope_variable"] = node["scope_variable"]

    # InvokeWorkflowFile-style nodes carry the same raw_path/target_relative_path/
    # argument_bindings/is_dynamic fields inline (in activity_flow) as they do in
    # the file-level `invokes` list (see _trim_invocation). When present, these
    # supersede the generic assignments/key_arguments/arguments handling below:
    # without this, such a node falls through to a leftover generic `arguments`
    # entry (typically a single misleading "Arguments=..." pseudo-property) instead
    # of the real per-parameter bindings - worse AND not even cheaper.
    if node.get("target_relative_path") or node.get("raw_path"):
        trimmed["target_relative_path"] = node.get("target_relative_path")
        trimmed["raw_path"] = node.get("raw_path")
        if node.get("is_dynamic"):
            trimmed["is_dynamic"] = True
        if node.get("argument_bindings"):
            trimmed["argument_bindings"] = [
                {
                    "argument_name": b.get("argument_name"),
                    "direction": b.get("direction"),
                    "expression": b.get("expression"),
                }
                for b in node["argument_bindings"]
            ]
    elif node.get("assignments"):
        trimmed["assignments"] = [
            {"to": a["to"], "value": a["value"]} for a in node["assignments"]
        ]
    elif node.get("key_arguments"):
        trimmed["key_arguments"] = [
            {"name": a["name"], "value": a["value"]} for a in node["key_arguments"]
        ]
    elif node.get("arguments"):
        trimmed["arguments"] = [
            {"name": a["name"], "value": a["value"]} for a in node["arguments"]
        ]

    # State-machine transitions (State activities in a StateMachine root) and the
    # is_final flag on the terminal state. This is the actual control-flow graph
    # for StateMachine-based projects (e.g. the standard REFramework Main.xaml) -
    # without it, each State just looks like an isolated block with no indication
    # of what triggers moving to which other state. Each transition's `action` is
    # itself an activity node, so it's put back through this same function (and,
    # like children, is subject to the purpose's drop_tags in _drop_tags_from_tree)
    # rather than special-cased.
    if node.get("is_final"):
        trimmed["is_final"] = True
    if node.get("transitions"):
        trimmed["transitions"] = [
            {
                "label": t.get("label"),
                "condition": t.get("condition") or None,
                "target": t.get("target"),
                "loops_back": bool(t.get("loops_back")),
                "action": _trim_activity_node(t["action"]) if t.get("action") else None,
            }
            for t in node["transitions"]
        ]

    children = [_trim_activity_node(c) for c in node.get("children", [])]
    if children:
        trimmed["children"] = children

    return trimmed


# ---------------------------------------------------------------------------
# Step 2: purpose-dependent shaping
# ---------------------------------------------------------------------------

def _drop_tags_from_tree(nodes: list[dict], tags: set[str]) -> list[dict]:
    """
    Removes activity nodes whose tag is in `tags`, anywhere in the tree (recursing
    into children and into every branch's steps). A dropped node's children are
    dropped with it - e.g. dropping RetryScope also drops what it retried, since a
    purpose that considers RetryScope noise generally considers its contents noise
    too. If a purpose ever needs "drop the wrapper but keep its contents", that's a
    different function - not needed by either purpose defined below.
    """
    result = []
    for node in nodes:
        if node.get("type") == "activity" and node.get("tag") in tags:
            if node.get("tag") in UNWRAP_TAGS:
                # Wrapper is noise, contents aren't - keep the children, drop only
                # this node (see UNWRAP_TAGS comment above).
                result.extend(_drop_tags_from_tree(node.get("children", []), tags))
            continue
        node = dict(node)
        if node.get("type") == "branch":
            node["branches"] = [
                {**b, "steps": _drop_tags_from_tree(b.get("steps", []), tags)}
                for b in node["branches"]
            ]
        else:
            if node.get("children"):
                node["children"] = _drop_tags_from_tree(node["children"], tags)
            if node.get("transitions"):
                new_transitions = []
                for t in node["transitions"]:
                    t = dict(t)
                    if t.get("action"):
                        dropped = _drop_tags_from_tree([t["action"]], tags)
                        t["action"] = dropped[0] if dropped else None
                    new_transitions.append(t)
                node["transitions"] = new_transitions
        result.append(node)
    return result


@dataclass
class Purpose:
    """
    One export "shape" - a named way of turning the trimmed catalog into a final
    payload. `drop_tags` is the common case (purpose-specific noise filtering);
    `extra_sources_filter` controls which extra_sources.py keys this purpose actually
    wants (e.g. a debug session doesn't need Config Settings values, a business doc
    doesn't need dependency package names).
    """

    key: str
    label: str
    drop_tags: set[str] = field(default_factory=set)
    include_project_json: bool = True
    include_config: bool = True

    def shape_file(self, trimmed_entry: dict) -> dict:
        if not self.drop_tags or "activity_flow" not in trimmed_entry:
            return trimmed_entry
        shaped = dict(trimmed_entry)
        shaped["activity_flow"] = _drop_tags_from_tree(trimmed_entry["activity_flow"], self.drop_tags)
        return shaped

    def shape_extras(self, extras: dict) -> dict:
        result = {}
        if self.include_project_json and extras.get("project_json"):
            result["project_json"] = extras["project_json"]
        if self.include_config and extras.get("config"):
            result["config"] = extras["config"]
        return result


# Registered purposes. Each purpose is a lens over the same trimmed catalog, not a
# separate extraction pass - see module docstring.
PURPOSES: dict[str, Purpose] = {
    "debug": Purpose(
        key="debug",
        label="Architecture review",
        drop_tags=set(),  # keep everything, including LogMessage/TryCatch/RetryScope -
                          # this purpose judges whether error-handling/retry/logging
                          # patterns are applied *consistently* across the project,
                          # which requires seeing where they are (and aren't) present
                          # in every file, not just one.
        include_project_json=True,
        include_config=True,
    ),
    "business_doc": Purpose(
        key="business_doc",
        label="Business / functional documentation",
        drop_tags=NOISE_TAGS,
        include_project_json=True,
        include_config=False,  # a functional description doesn't need raw config values
    ),
}


# Purposes for the *single-file* export (see build_single_file_export below), kept in
# their own registry rather than added into PURPOSES above: PURPOSES drives both the
# whole-project modal's option list (derived from AI_EXPORTS' keys) and
# build_all_exports' full-project pass, and a file-scoped purpose belongs in neither -
# it would otherwise (a) show up as a bogus "analyze the whole project" option in the
# project modal, and (b) get built once per project unnecessarily. include_project_json
# / include_config are irrelevant here: build_single_file_export never touches
# extra_sources.py output at all (see its docstring) - a single file's export is scoped
# to that file (plus thin neighbor signatures), not the project's global metadata.
FILE_PURPOSES: dict[str, Purpose] = {
    "file_summary": Purpose(
        key="file_summary",
        label="Summarize this file",
        drop_tags=NOISE_TAGS,  # same reasoning as business_doc above: a reader who
                                # wants to understand what this file does and how its
                                # logic flows doesn't need LogMessage/RetryScope/
                                # TryCatch/Delay/CommentOut cluttering that reading -
                                # they're operational plumbing, not business logic.
    ),
    "file_debug": Purpose(
        key="file_debug",
        label="Debug this file",
        drop_tags=set(),  # no noise filtering - diagnosing a problem is exactly when
                          # LogMessage/RetryScope/TryCatch/Delay stop being noise and
                          # become the evidence (or the gap in evidence) the
                          # diagnosis depends on. Same reasoning as "debug" above,
                          # just scoped to one file instead of the whole project.
    ),
}


# ---------------------------------------------------------------------------
# Step 3: rendering a shaped file entry as a text tree (see module docstring,
# point 3). Consumes exactly the `shaped` dict that Purpose.shape_file() returns -
# does not re-decide what to keep.
# ---------------------------------------------------------------------------

INDENT = "  "


def _fmt_value(v) -> str:
    """Collapses a multi-line VB expression (e.g. a Code activity's script body)
    onto one line for the tree view - the full text is still there, just not
    breaking the tree's line-per-node shape. Only actual newlines are escaped."""
    if v is None:
        return ""
    return str(v).replace("\r\n", "\\n").replace("\n", "\\n")


def _render_args(label: str, args: list[dict]) -> str:
    """Renders a compact 'Name=Value, Name=Value' suffix for assignments /
    key_arguments / arguments - whichever one is present on the node (they're
    mutually exclusive after _trim_activity_node)."""
    if not args:
        return ""
    parts = [f"{a.get('to', a.get('name'))}={_fmt_value(a.get('value'))}" for a in args]
    return f"  [{label}: " + ", ".join(parts) + "]"


def _render_node(node: dict, depth: int, out: list[str]) -> None:
    pad = INDENT * depth

    if node.get("type") == "branch":
        kind = node.get("kind", "branch")
        cond = node.get("condition")
        header = f"{pad}{kind.upper()}" + (f" [{cond}]" if cond else "")
        if node.get("key_arguments"):
            header += _render_args("args", node["key_arguments"])
        if node.get("scope_variable"):
            header += f"  as {node['scope_variable']}"
        if node.get("annotation"):
            header += f"  # {node['annotation']}"
        out.append(header)
        for b in node["branches"]:
            blabel = b["label"]
            bcond = f" [{b['condition']}]" if b.get("condition") else ""
            out.append(f"{pad}{INDENT}- {blabel}{bcond}:")
            if not b.get("steps"):
                out.append(f"{pad}{INDENT * 2}(empty)")
            for step in b.get("steps", []):
                _render_node(step, depth + 3, out)
        return

    # activity node - display name sometimes carries a trailing "- " with nothing
    # after it (a quirk of some UiPath activities with no custom label); strip it
    # rather than reproduce it verbatim.
    name = (node.get("name") or "").rstrip(" -") or node.get("tag")
    line = f"{pad}{name}"
    if node.get("tag") and node.get("tag") != node.get("name"):
        line += f"  ({node['tag']})"
    if node.get("scope_variable"):
        line += f"  as {node['scope_variable']}"

    target = node.get("target_relative_path") or node.get("raw_path")
    if target:
        line += f"  -> {target}"
        if node.get("is_dynamic"):
            line += "  (dynamic path)"
        bindings = node.get("argument_bindings") or []
        if bindings:
            bind_str = ", ".join(
                f"{b.get('argument_name')}({b.get('direction')})={_fmt_value(b.get('expression'))}"
                for b in bindings
            )
            line += f"  [{bind_str}]"
    elif node.get("assignments"):
        line += _render_args("set", node["assignments"])
    elif node.get("key_arguments"):
        line += _render_args("args", node["key_arguments"])
    elif node.get("arguments"):
        line += _render_args("args", node["arguments"])

    if node.get("is_final"):
        line += "  [FINAL]"

    if node.get("annotation"):
        line += f"  # {node['annotation']}"

    out.append(line)

    for child in node.get("children", []):
        _render_node(child, depth + 1, out)

    # State-machine transitions: one compact arrow line per transition, e.g.
    #   -> Get Transaction Data  [SystemException is Nothing]  (Successful)
    # with a loop marker when the target is a state already seen earlier in the
    # graph (a retry/repeat cycle), and the triggered action (if any, and if not
    # dropped by the purpose's noise filtering) rendered as a nested line rather
    # than inlined, since it can itself be a multi-step sequence.
    for t in node.get("transitions") or []:
        tline = f"{pad}{INDENT}-> {t.get('target')}"
        if t.get("condition"):
            tline += f"  [{t['condition']}]"
        if t.get("label"):
            tline += f"  ({t['label']})"
        if t.get("loops_back"):
            tline += "  (loop)"
        out.append(tline)
        if t.get("action"):
            _render_node(t["action"], depth + 2, out)


def render_activity_flow(shaped_flow: list[dict]) -> str:
    """Takes shaped_entry['activity_flow'] (already trimmed AND purpose-shaped)
    and returns it as an indented text tree."""
    if not shaped_flow:
        return "(no activities)"
    out: list[str] = []
    for node in shaped_flow:
        _render_node(node, 0, out)
    return "\n".join(out)


def render_file_entry(shaped: dict, raw_path: str) -> str:
    """Renders one file's full section (header: args/vars/invokes, then the flow
    tree) - the replacement for the '### path\\n```json ... ```' block."""
    lines = [f"### {shaped.get('relative_path', raw_path)}"]

    if not shaped.get("parse_ok", True):
        lines.append("(failed to parse)")
        return "\n".join(lines)

    args = shaped.get("arguments") or []
    if args:
        arg_strs = [f"{a['name']} ({a.get('direction', '')} {a['type']})".strip() for a in args]
        lines.append("Args: " + ", ".join(arg_strs))

    variables = shaped.get("variables") or []
    if variables:
        var_strs = [f"{v['name']} ({v['type']})" for v in variables]
        lines.append("Vars: " + ", ".join(var_strs))

    invokes = shaped.get("invokes") or []
    if invokes:
        # Bare dependency list only - which files this one calls. The actual
        # arguments passed at each call site are rendered inline in the Flow
        # section below (right where the call happens), not duplicated here.
        inv_strs = []
        for inv in invokes:
            target = inv.get("target_relative_path") or inv.get("raw_path")
            if inv.get("is_dynamic"):
                target += "  (dynamic path)"
            inv_strs.append(target)
        lines.append("Invokes: " + ", ".join(inv_strs))

    invoked_by = shaped.get("invoked_by") or []
    if invoked_by:
        lines.append("Invoked by: " + ", ".join(invoked_by))

    lines.append("")
    lines.append("Flow:")
    lines.append(render_activity_flow(shaped.get("activity_flow", [])))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 4: assembling the final text payload
# ---------------------------------------------------------------------------

def _render_project_json(pj: dict) -> str:
    lines = ["## Project metadata (project.json)"]
    if pj.get("main"):
        lines.append(f"- Entry point (main): {pj['main']}")
    deps = pj.get("dependencies") or {}
    if deps:
        lines.append(f"- Installed activity packages: {', '.join(sorted(deps.keys()))}")
    if pj.get("entry_points"):
        lines.append(f"- Entry points: {json.dumps(pj['entry_points'], ensure_ascii=False)}")
    return "\n".join(lines)


def _render_config(cfg: dict) -> str:
    lines = [f"## Config workbook ({cfg.get('source_file', 'Config.xlsx')})"]
    if cfg.get("settings"):
        lines.append("### Settings")
        for row in cfg["settings"]:
            lines.append(f"- {row['name']} = {row['value']}")
    if cfg.get("assets"):
        lines.append("### Assets (Orchestrator asset references, not secret values)")
        for row in cfg["assets"]:
            lines.append(f"- {row['name']}")
    return "\n".join(lines)


def build_ai_export(catalog: dict, extras: dict, purpose_key: str) -> str:
    """
    Main entry point. Combines the scanner.py catalog and extra_sources.py output
    into a single text block shaped for the given purpose.

    Raises KeyError if purpose_key isn't in PURPOSES - callers (the report.py /
    modal wiring) are expected to only offer registered purpose keys, so an unknown
    key here means a wiring bug, not bad user input to recover from gracefully.
    """
    purpose = PURPOSES[purpose_key]

    sections = [f"# UiPath project export ({purpose.label})", ""]

    extras_shaped = purpose.shape_extras(extras)
    if "project_json" in extras_shaped:
        sections.append(_render_project_json(extras_shaped["project_json"]))
        sections.append("")
    if "config" in extras_shaped:
        sections.append(_render_config(extras_shaped["config"]))
        sections.append("")

    sections.append(f"## Workflow files ({catalog.get('total_xaml_files', 0)} total)")
    sections.append("")

    for entry in catalog.get("files", []):
        trimmed = _trim_file_entry(entry)
        shaped = purpose.shape_file(trimmed)
        sections.append(render_file_entry(shaped, entry.get("relative_path")))
        sections.append("")

    if catalog.get("unresolved_invocations"):
        sections.append("## Unresolved invocations (target file not found)")
        for u in catalog["unresolved_invocations"]:
            sections.append(f"- {u['from_file']} -> \"{u['raw_path']}\"")
        sections.append("")

    return "\n".join(sections)


def _find_entry(catalog: dict, relative_path: str) -> dict | None:
    return next((f for f in catalog.get("files", []) if f["relative_path"] == relative_path), None)


def _signature_line(catalog: dict, relative_path: str) -> str:
    """
    Renders one neighbor file as "path: arg (dir type), arg (dir type)" - its public
    contract only, not its flow. Used for the Invokes/Invoked-by lists in a
    single-file export so the AI has some sense of what a neighbor expects/returns
    without pulling in that neighbor's full activity tree (which would just turn a
    "deep dive on one file" purpose back into a whole-project export).
    """
    entry = _find_entry(catalog, relative_path)
    if entry is None:
        # Can happen for an unresolved/dynamic invocation target - nothing to look up.
        return relative_path
    args = entry.get("arguments") or []
    if not args:
        return f"{relative_path} (no arguments)"
    arg_strs = [f"{a['name']} ({a.get('direction', '')} {a['type']})".strip() for a in args]
    return f"{relative_path}: " + ", ".join(arg_strs)


def build_single_file_export(catalog: dict, relative_path: str, purpose_key: str = "file_summary") -> str:
    """
    Builds a compact export for exactly one workflow file, for the drawer's "Ask AI
    about this file" button (see ai_panel.html / drawer_fragment.html wiring).

    Deliberately reuses the *same* _trim_file_entry / shape_file / render_file_entry
    pipeline as the whole-project export - see the architecture discussion this
    answers: the catalog is the single source of truth, and a single-file export is
    just a different lens over one entry of it, not a second extraction pass. This
    function never touches extra_sources.py output (project.json / Config.xlsx) -
    those are project-wide metadata, out of scope for a file-scoped export.

    Raises KeyError if relative_path isn't in the catalog, or if purpose_key isn't
    registered in FILE_PURPOSES - both indicate a wiring bug in the caller (the UI is
    expected to only ever pass a path it already has from the loaded catalog), not
    user input to validate defensively, matching build_ai_export's contract above.
    """
    purpose = FILE_PURPOSES[purpose_key]
    entry = _find_entry(catalog, relative_path)
    if entry is None:
        raise KeyError(f"File not found in catalog: {relative_path}")

    trimmed = _trim_file_entry(entry)
    shaped = purpose.shape_file(trimmed)
    sections = [render_file_entry(shaped, relative_path)]

    # Neighbor signatures: same raw_path/target lists the file section already prints
    # under "Invokes:"/"Invoked by:", but expanded here with each neighbor's
    # arguments, so the AI can reason about the data actually crossing each call
    # boundary without needing that neighbor's full export.
    invokes = shaped.get("invokes") or []
    invoked_by = shaped.get("invoked_by") or []
    if invokes or invoked_by:
        sections.append("")
        sections.append("## Neighboring files (signatures only, not full flow)")
        for inv in invokes:
            target = inv.get("target_relative_path") or inv.get("raw_path")
            if target:
                sections.append(f"- Calls -> {_signature_line(catalog, target)}")
        for caller in invoked_by:
            sections.append(f"- Called by <- {_signature_line(catalog, caller)}")

    return "\n".join(sections)


def build_all_exports(catalog: dict, extras: dict) -> dict[str, str]:
    """Convenience wrapper: builds the export text for every registered purpose at
    once, keyed by purpose key - what report.py needs to embed all of them into the
    generated HTML in one pass (see architecture discussion: AI_EXPORTS[purpose])."""
    return {key: build_ai_export(catalog, extras, key) for key in PURPOSES}


def build_all_file_exports(catalog: dict) -> dict[str, dict[str, str]]:
    """
    Convenience wrapper for report.py: builds every FILE_PURPOSES export for every
    file in the catalog up front, keyed as {relative_path: {purpose_key: text}} -
    what gets embedded as the AI_FILE_EXPORTS global for ai_panel.html to read
    synchronously when the drawer's "Ask AI about this file" button is clicked
    (same "precompute at report-generation time" approach already used for
    AI_EXPORTS, so the button's payload is instant and requires no server/runtime
    dependency on scanner.py or a live Python process).

    Only failed-to-parse files are skipped: _trim_file_entry passes them through
    unchanged (see its docstring) but they have no "arguments"/"activity_flow" to
    build a meaningful single-file export from, and the drawer's "Parse Error"
    badge already tells the user why - no export button is needed for them.
    """
    result: dict[str, dict[str, str]] = {}
    for entry in catalog.get("files", []):
        if not entry.get("parse_ok"):
            continue
        path = entry["relative_path"]
        result[path] = {
            key: build_single_file_export(catalog, path, key) for key in FILE_PURPOSES
        }
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python ai_export.py <path-to-scanner-catalog.json> [project_root]")
        sys.exit(1)

    catalog_path = Path(sys.argv[1])
    with open(catalog_path, "r", encoding="utf-8") as f:
        loaded_catalog = json.load(f)

    project_root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(loaded_catalog.get("project_root", "."))

    from extra_sources import build_extra_sources
    loaded_extras = build_extra_sources(project_root)

    for p_key, text in build_all_exports(loaded_catalog, loaded_extras).items():
        out_path = catalog_path.with_name(f"ai_export_{p_key}.md")
        out_path.write_text(text, encoding="utf-8")
        print(f"{p_key}: {len(text)} chars -> {out_path}")