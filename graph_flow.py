"""
UiPath XAML Scanner - Graph Flow Extraction (Flowchart & StateMachine)

Split out of scanner.py: everything that reconstructs a Flowchart's or StateMachine's
*visual* execution order by walking their node graphs, as opposed to the plain recursive
descent scanner.py uses for tree-shaped activities (Sequence/If/Switch/TryCatch).

Both root-activity kinds share the same underlying shape in XAML - nodes wired together via
x:Reference pointers, in either a FLAT form (every node is a direct child, referenced by
name) or a NESTED form (.NET's object-graph-sharing form: a shared node is written inline at
its first occurrence and referenced as a bare back-pointer everywhere else) - so they share
the same graph-walking primitives here (_get_reference_and_inline_elem, _resolve_flow_ref,
_parse_initial_state_attr).

Public interface (called from scanner.py's extract_activity_flow):
  - extract_flowchart_steps(flowchart_elem, ctx) -> list[dict]
  - extract_statemachine_steps(statemachine_elem, ctx) -> list[dict]

Dependency direction: this module imports shared XML/activity helpers and FlowCtx from
scanner.py (local_name, _get_annotation, _get_node_id, _get_real_children,
_process_activity_node). scanner.py does NOT import this module at the top level - it
imports extract_flowchart_steps/extract_statemachine_steps lazily, inside
extract_activity_flow, to avoid a circular import (scanner.py <-> graph_flow.py).
"""

import re
import xml.etree.ElementTree as ET

from scanner import (
    ACTIVITIES_NS,
    X_NS,
    FlowCtx,
    local_name,
    _get_annotation,
    _get_node_id,
    _get_real_children,
    _process_activity_node,
)


# --- Flowchart support -----------------------------------------------------------------
# Unlike Sequence/If/Switch/TryCatch, a Flowchart is not a nested tree in the XML - it's a
# graph. FlowStep/FlowDecision/FlowSwitch nodes are wired together with x:Reference pointers
# (Flowchart.StartNode, FlowStep.Next, FlowDecision.True/False, FlowSwitch.Cases/Default).
# The functions below walk that graph from the start node so the extracted order matches
# what Studio actually draws, instead of raw XML declaration order.
#
# Two shapes of x:Name/x:Reference wiring show up in the wild:
#  - FLAT: every flow node is a direct child of <Flowchart>, each carrying its own x:Name,
#    and every Next/True/False/Case/Default property points at one via <x:Reference>.
#  - NESTED (.NET XAML's object-graph-sharing form, seen when the same node is reachable
#    from more than one place - e.g. a branch merge or a loop back-edge): only the FIRST
#    occurrence of a shared node in document order carries x:Name, and it is written
#    INLINE at that first occurrence (e.g. nested inside another FlowDecision's
#    FlowDecision.True), not as a direct child of Flowchart. Every subsequent pointer to
#    that same node - including the one at the position where a flat file would have put
#    the full node - is collapsed to a bare <Reference>name</Reference> back-pointer, which
#    is not itself a node and carries no x:Name of its own. A file can mix both shapes.
# Recursing through the whole subtree (rather than only direct children) collects flow
# nodes regardless of which shape produced them, so the walk below never has to care.

_FLOW_NODE_TAGS = {"FlowStep", "FlowDecision", "FlowSwitch"}
# StateMachine's node tag - referenced here (not just in the StateMachine section further
# down) because _get_reference_and_inline_elem/_resolve_flow_ref below are shared by both
# Flowchart and StateMachine graph walks, and need to recognize an inline <State> the same
# way they recognize an inline FlowStep/FlowDecision/FlowSwitch.
_STATE_TAG = "State"
_X_NAME_ATTR = f"{{{X_NS}}}Name"


def _build_flowchart_node_map(flowchart_elem: ET.Element) -> dict[str, ET.Element]:
    """
    Maps each flow node's x:Name to its element, wherever in the subtree it's actually
    declared. Must recurse (not just look at direct children of <Flowchart>) because the
    nested x:Reference form (see module note above) declares a node's only real copy
    inline inside a sibling node's True/False/Next/Case/Default branch, potentially several
    levels deep - a direct-children-only scan would miss every node that isn't a "first
    occurrence" living straight under <Flowchart>.
    """
    node_map: dict[str, ET.Element] = {}
    for elem in flowchart_elem.iter():
        if elem is flowchart_elem:
            continue
        if local_name(elem.tag) in _FLOW_NODE_TAGS:
            name = elem.get(_X_NAME_ATTR)
            if name and name not in node_map:
                node_map[name] = elem
    return node_map


def _get_reference_and_inline_elem(
    container_elem: ET.Element | None,
) -> tuple[str | None, ET.Element | None]:
    """
    Reads the target of a flow-graph pointer property - a Flowchart pointer (FlowStep.Next,
    FlowDecision.True/False, FlowSwitch.Default, a FlowSwitch.Cases entry) or a
    StateMachine pointer (Transition.To, StateMachine.InitialState) - returning
    (name, inline_elem). Handles both shapes UiPath/.NET XAML emits (see module note above
    _FLOW_NODE_TAGS):
    - FLAT form: a bare <x:Reference>name</x:Reference> child, or the container itself
      being that Reference (FlowSwitch.Cases entries) - returns (name, None).
    - NESTED form: the container holds the actual node INLINE (a <FlowStep>/
      <FlowDecision>/<FlowSwitch>/<State>, not a Reference) because this is that node's
      first - and only fully-written - occurrence in the document. Returns its x:Name (if
      any) alongside the element itself, so a first occurrence with no x:Name at all (legal
      in XAML for a node only ever pointed to from a single place) can still be resolved by
      the caller falling back to the element rather than a name-map lookup that would
      otherwise fail.
    """
    if container_elem is None:
        return None, None
    if local_name(container_elem.tag) == "Reference":
        return (container_elem.text or "").strip() or None, None
    for child in container_elem:
        child_tag = local_name(child.tag)
        if child_tag == "Reference":
            return (child.text or "").strip() or None, None
        if child_tag in _FLOW_NODE_TAGS or child_tag == _STATE_TAG:
            name = child.get(_X_NAME_ATTR)
            return name, child
    return None, None


def _extract_condition_expression(container_elem: ET.Element | None) -> str:
    """
    Extracts condition/expression text from a property element, handling both forms
    UiPath emits: a simple InArgument/OutArgument wrapper with the expression as text
    content (the common case for <If Condition="...">), and a VisualBasicValue /
    VisualBasicReference / CSharpValue wrapper (the common case for FlowDecision.Condition
    and FlowSwitch.Expression) whose expression lives in an ExpressionText attribute
    instead of the element's text.
    """
    if container_elem is None:
        return ""
    wrapper = next(iter(container_elem), None)
    if wrapper is None:
        return (container_elem.text or "").strip()
    text = (wrapper.text or "").strip()
    if text:
        return text
    return (wrapper.get("ExpressionText") or "").strip()


def _resolve_flow_ref(container_elem: ET.Element | None, node_map: dict[str, ET.Element]) -> str | None:
    """
    Resolves a flow-graph pointer property to a name key in node_map, handling both the
    flat (<x:Reference>) and nested (inline node, see module note above _FLOW_NODE_TAGS)
    forms via _get_reference_and_inline_elem. An inline node with a real x:Name is already
    in node_map (it was picked up by _build_flowchart_node_map's recursive scan) so its
    name can be returned directly. The rare inline node with NO x:Name at all (legal in
    XAML for a node only ever pointed to from a single place) isn't in node_map under any
    name yet - it's registered here under a synthetic key derived from its own node id, so
    every other part of the walk (loop-back detection, in-degree, visited-tracking) can go
    on treating "current" as a plain name key without needing to special-case elements.
    """
    name, inline_elem = _get_reference_and_inline_elem(container_elem)
    if name:
        return name
    if inline_elem is not None:
        synthetic = f"__inline::{_get_node_id(inline_elem, local_name(inline_elem.tag))}"
        node_map.setdefault(synthetic, inline_elem)
        return synthetic
    return None


def _flowchart_outgoing_refs(elem: ET.Element, node_map: dict[str, ET.Element]) -> list[str]:
    """All node names a flow node can lead to next - used only to compute in-degrees so
    branch merge points can be detected (see _walk_flowchart_branch)."""
    tag = local_name(elem.tag)
    refs: list[str] = []
    if tag == "FlowStep":
        ref = _resolve_flow_ref(elem.find(f"{{{ACTIVITIES_NS}}}FlowStep.Next"), node_map)
        if ref:
            refs.append(ref)
    elif tag == "FlowDecision":
        for prop in ("True", "False"):
            ref = _resolve_flow_ref(elem.find(f"{{{ACTIVITIES_NS}}}FlowDecision.{prop}"), node_map)
            if ref:
                refs.append(ref)
    elif tag == "FlowSwitch":
        cases_elem = elem.find(f"{{{ACTIVITIES_NS}}}FlowSwitch.Cases")
        if cases_elem is not None:
            for case_child in cases_elem:
                ref = _resolve_flow_ref(case_child, node_map)
                if ref:
                    refs.append(ref)
        default_ref = _resolve_flow_ref(elem.find(f"{{{ACTIVITIES_NS}}}FlowSwitch.Default"), node_map)
        if default_ref:
            refs.append(default_ref)
    return refs


def _walk_flowchart_branch(
    start_name: str | None,
    node_map: dict[str, ET.Element],
    in_degree: dict[str, int],
    visited: set[str],
    ctx: FlowCtx,
) -> tuple[list[dict], str | None]:
    """
    Walks a single path through the flowchart graph starting at start_name, following
    Next/True/False/Case/Default links in visual execution order. Returns (steps, resume_at):
    - resume_at is a node name if the walk stopped at a JOIN POINT - a node with more than
      one incoming edge, e.g. where an if/else's two branches reconnect - so the caller can
      continue the outer walk from there exactly once, instead of every branch independently
      re-walking (and duplicating) everything downstream of the merge.
    - resume_at is None if the path simply ended (no outgoing reference), or looped back to
      an already-visited node (a real loop, e.g. a retry pattern) - in the loop case a
      synthetic "loops back to" marker step is appended so the cycle is still visible rather
      than silently truncated.
    """
    steps: list[dict] = []
    current = start_name
    while current is not None:
        if current in visited:
            target_elem = node_map.get(current)
            target_label = target_elem.get("DisplayName") if target_elem is not None else None
            steps.append(
                {
                    "type": "activity",
                    "tag": "FlowLoopBack",
                    "id": f"loopback::{current}",
                    "name": "\u21ba Loops back to: " + (target_label or current),
                    "children": [],
                }
            )
            return steps, None

        if current != start_name and in_degree.get(current, 0) > 1:
            return steps, current

        visited.add(current)
        elem = node_map.get(current)
        if elem is None:
            return steps, None

        tag = local_name(elem.tag)
        if tag == "FlowStep":
            body = next(iter(_get_real_children(elem)), None)
            if body is not None:
                step_elem, scope_var = body
                node = _process_activity_node(step_elem, ctx)
                if scope_var:
                    node["scope_variable"] = scope_var
                steps.append(node)
            current = _resolve_flow_ref(elem.find(f"{{{ACTIVITIES_NS}}}FlowStep.Next"), node_map)

        elif tag == "FlowDecision":
            condition = _extract_condition_expression(
                elem.find(f"{{{ACTIVITIES_NS}}}FlowDecision.Condition")
            ) or (elem.get("Condition") or "").strip()
            true_ref = _resolve_flow_ref(elem.find(f"{{{ACTIVITIES_NS}}}FlowDecision.True"), node_map)
            false_ref = _resolve_flow_ref(elem.find(f"{{{ACTIVITIES_NS}}}FlowDecision.False"), node_map)

            true_steps, true_join = _walk_flowchart_branch(true_ref, node_map, in_degree, visited, ctx)
            false_steps, false_join = _walk_flowchart_branch(false_ref, node_map, in_degree, visited, ctx)

            branch_node = {
                "type": "branch",
                "kind": "flow-if",
                "id": _get_node_id(elem, "FlowDecision"),
                "condition": condition,
                "condition_refs": ctx.var_refs(condition, "read"),
                "branches": [
                    {"label": "True", "condition": None, "condition_refs": [], "steps": true_steps},
                    {"label": "False", "condition": None, "condition_refs": [], "steps": false_steps},
                ],
            }
            annotation = _get_annotation(elem)
            if annotation:
                branch_node["annotation"] = annotation
            steps.append(branch_node)

            # Resume the outer walk at the shared join point only if both branches actually
            # reconnect at the same node; otherwise (different, or no, join) the flow simply
            # ends here from this path's point of view.
            current = true_join if true_join and true_join == false_join else None

        elif tag == "FlowSwitch":
            condition = _extract_condition_expression(
                elem.find(f"{{{ACTIVITIES_NS}}}FlowSwitch.Expression")
            ) or (elem.get("Expression") or "").strip()
            key_attr = f"{{{X_NS}}}Key"

            branches = []
            join_candidates: list[str | None] = []
            cases_elem = elem.find(f"{{{ACTIVITIES_NS}}}FlowSwitch.Cases")
            if cases_elem is not None:
                for case_child in cases_elem:
                    case_ref = _resolve_flow_ref(case_child, node_map)
                    case_steps, case_join = _walk_flowchart_branch(case_ref, node_map, in_degree, visited, ctx)
                    branches.append(
                        {
                            "label": f"Case {case_child.get(key_attr, '?')}",
                            "condition": None,
                            "condition_refs": [],
                            "steps": case_steps,
                        }
                    )
                    join_candidates.append(case_join)

            default_ref = _resolve_flow_ref(elem.find(f"{{{ACTIVITIES_NS}}}FlowSwitch.Default"), node_map)
            default_steps, default_join = _walk_flowchart_branch(default_ref, node_map, in_degree, visited, ctx)
            branches.append({"label": "Default", "condition": None, "condition_refs": [], "steps": default_steps})
            join_candidates.append(default_join)

            branch_node = {
                "type": "branch",
                "kind": "flow-switch",
                "id": _get_node_id(elem, "FlowSwitch"),
                "condition": condition,
                "condition_refs": ctx.var_refs(condition, "read"),
                "branches": branches,
            }
            annotation = _get_annotation(elem)
            if annotation:
                branch_node["annotation"] = annotation
            steps.append(branch_node)

            unique_joins = {j for j in join_candidates if j}
            current = next(iter(unique_joins)) if len(unique_joins) == 1 else None

        else:
            # Unrecognized flow-node kind - stop gracefully rather than crashing the scan.
            return steps, None

    return steps, None


_X_REFERENCE_MARKUP_RE = re.compile(r"^\{x:Reference\s+([^\s}]+)\}$")


def _parse_initial_state_attr(raw_value: str) -> str | None:
    """
    Parses a start-node value when it's written as a plain XML attribute (e.g.
    InitialState="{x:Reference name}" on <StateMachine>, or StartNode="{x:Reference name}"
    on <Flowchart>) using XAML markup-extension syntax, rather than as a
    <StateMachine.InitialState>/<Flowchart.StartNode> property element. {x:Reference name}
    is the curly-brace markup-extension shorthand for the same x:Reference pointer used
    everywhere else in the file - just written inline as an attribute string instead of a
    child element, so it needs its own small parse instead of going through
    _resolve_flow_ref (which only understands the element-based forms). Shared by both
    Flowchart and StateMachine extraction since Studio can emit either shorthand on either
    root activity type.
    """
    match = _X_REFERENCE_MARKUP_RE.match(raw_value.strip())
    return match.group(1) if match else None


def extract_flowchart_steps(flowchart_elem: ET.Element, ctx: FlowCtx) -> list[dict]:
    """
    Reconstructs a Flowchart's visual execution order by walking its node graph from the
    start node (see module comment above _FLOW_NODE_TAGS for why this can't just be a
    document-order pass like Sequence/If/Switch). As with StateMachine's InitialState (see
    _extract_statemachine_steps), the start node can be written either as a
    <Flowchart.StartNode> property element or as a StartNode="{x:Reference name}" attribute
    directly on <Flowchart> - both are checked, attribute form first since that's what
    Studio actually emits when it uses this shorthand at all.
    """
    node_map = _build_flowchart_node_map(flowchart_elem)
    if not node_map:
        return []

    in_degree: dict[str, int] = {}
    for elem in list(node_map.values()):
        for ref in _flowchart_outgoing_refs(elem, node_map):
            in_degree[ref] = in_degree.get(ref, 0) + 1

    start_ref = _parse_initial_state_attr(flowchart_elem.get("StartNode") or "")
    if start_ref is None:
        start_ref = _resolve_flow_ref(flowchart_elem.find(f"{{{ACTIVITIES_NS}}}Flowchart.StartNode"), node_map)
    if start_ref is None or start_ref not in node_map:
        # No usable declared start - fall back to a node nothing else points to, if any.
        start_ref = next((name for name in node_map if in_degree.get(name, 0) == 0), next(iter(node_map)))

    visited: set[str] = set()
    steps, _ = _walk_flowchart_branch(start_ref, node_map, in_degree, visited, ctx)

    # Anything never reached by following the graph from the start (dead/orphaned nodes)
    # still deserves to show up somewhere rather than silently vanishing from the map.
    for name in list(node_map):
        if name not in visited:
            extra_steps, _ = _walk_flowchart_branch(name, node_map, in_degree, visited, ctx)
            steps.extend(extra_steps)

    return steps


# --- StateMachine support ----------------------------------------------------------------
# Like Flowchart, a StateMachine is a graph, not a nested tree: State elements are wired
# together via State.Transitions -> Transition.To pointers, using the exact same flat
# (x:Reference) / nested (inline first-occurrence, see module note above _FLOW_NODE_TAGS)
# shapes a Flowchart uses for its own node graph. The functions below mirror the Flowchart
# walk, adapted for state-machine semantics: each state is visited once (states don't need
# join-point merging the way If/Switch branches do - a transition is just a labeled edge
# out of a state, not a fork that needs to reconnect), and a state's own body (State.Entry)
# is processed as ordinary nested content.
# (_STATE_TAG itself is defined earlier, alongside _FLOW_NODE_TAGS, since the shared
# _get_reference_and_inline_elem/_resolve_flow_ref helpers there need it too.)


def _build_state_node_map(statemachine_elem: ET.Element) -> dict[str, ET.Element]:
    """
    Maps each State's x:Name to its element, wherever in the subtree it's actually
    declared - mirrors _build_flowchart_node_map's recursive scan, since States are shared
    via the same flat/nested x:Reference shapes Flowchart nodes use (a state reachable from
    more than one transition has its one real copy written inline inside the first
    Transition.To that reaches it, with every other pointer to it collapsed to a bare
    <Reference>).
    """
    node_map: dict[str, ET.Element] = {}
    for elem in statemachine_elem.iter():
        if elem is statemachine_elem:
            continue
        if local_name(elem.tag) == _STATE_TAG:
            name = elem.get(_X_NAME_ATTR)
            if name and name not in node_map:
                node_map[name] = elem
    return node_map


def _get_state_body(state_elem: ET.Element, prop_suffix: str) -> ET.Element | None:
    """Returns the activity inside a State.Entry or State.Exit property element, if any."""
    for child in state_elem:
        if local_name(child.tag) == f"State.{prop_suffix}":
            return next(iter(child), None)
    return None


def _extract_state_transitions(state_elem: ET.Element) -> list[ET.Element]:
    """Returns this State's outgoing <Transition> elements, in document order."""
    for child in state_elem:
        if local_name(child.tag) == "State.Transitions":
            return [t for t in child if local_name(t.tag) == "Transition"]
    return []


def _wrap_labeled_node(node: dict, label: str) -> dict:
    """
    Prefixes a processed activity node's display name with a label (e.g. "Entry: " /
    "Exit: ") for State.Entry/State.Exit bodies. _process_activity_node can return either
    an "activity" node (has "name") or a "branch" node for If/Switch/TryCatch root
    activities (no "name" - branch nodes are labeled by their branches instead), so this
    only rewrites "name" when present rather than assuming every returned node has one.
    """
    if "name" in node:
        return {**node, "name": f"{label}: {node['name']}"}
    return {**node, "label_prefix": label}


def _walk_state_machine(
    start_name: str,
    node_map: dict[str, ET.Element],
    ctx: FlowCtx,
    visited: set[str],
) -> list[dict]:
    """
    Walks the state graph depth-first from start_name, emitting one "state" node per State
    (with its own id, entry/exit body, and outgoing transitions) the first time it's
    reached - mirroring _walk_flowchart_branch, which recurses into a branch immediately
    rather than queuing it for later.

    This MUST be depth-first, not breadth-first: with a plain BFS queue, a state with an
    early error-path transition to a "terminal" state (e.g. Initialization's "System
    Exception" -> End Process, listed before its "Successful" -> Get Transaction Data) gets
    that terminal state queued before the main path's next step is even discovered - so End
    Process would end up emitted BEFORE Process Transaction despite being reached later in
    the actual flow. Recursing into each not-yet-visited transition target immediately
    (in document order) instead produces the order a human reading the state machine would
    expect: Initialization, Get Transaction Data, Process Transaction, End Process - each
    state's "main" continuation appears right after it, with only genuine loop-backs (to a
    state already on the current path) deferred as lightweight markers instead of full nodes.

    A transition back to an already-emitted state becomes a lightweight "loops back to" edge
    instead of a full state node, so cycles (a very common state-machine pattern - e.g.
    "process next item" looping back to itself) terminate the walk instead of recursing
    forever. visited is shared with and mutated for the caller, so a second call starting
    from an orphaned state (see extract_statemachine_steps) never re-emits a state this call
    already did.
    """
    if start_name in visited:
        return []
    visited.add(start_name)

    elem = node_map.get(start_name)
    if elem is None:
        return []

    node_id = _get_node_id(elem, "State")
    state_node: dict = {
        "type": "activity",
        "tag": "State",
        "name": elem.get("DisplayName", "") or "State",
        "id": node_id,
        "children": [],
    }
    if (elem.get("IsFinal") or "").strip().lower() == "true":
        state_node["is_final"] = True

    annotation = _get_annotation(elem)
    if annotation:
        state_node["annotation"] = annotation

    entry_elem = _get_state_body(elem, "Entry")
    if entry_elem is not None:
        state_node["children"].append(
            _wrap_labeled_node(_process_activity_node(entry_elem, ctx), "Entry")
        )

    exit_elem = _get_state_body(elem, "Exit")
    if exit_elem is not None:
        state_node["children"].append(
            _wrap_labeled_node(_process_activity_node(exit_elem, ctx), "Exit")
        )

    transitions_out = []
    nested_steps: list[dict] = []
    for transition_elem in _extract_state_transitions(elem):
        condition = _extract_condition_expression(
            transition_elem.find(f"{{{ACTIVITIES_NS}}}Transition.Condition")
        ) or (transition_elem.get("Condition") or "").strip()

        # Transition.Action holds an optional activity run when the transition fires.
        action_elem = None
        for child in transition_elem:
            if local_name(child.tag) == "Transition.Action":
                action_elem = next(iter(child), None)

        to_container = None
        for child in transition_elem:
            if local_name(child.tag) == "Transition.To":
                to_container = child
                break
        to_name = _resolve_flow_ref(to_container, node_map)

        transition_info: dict = {
            "label": transition_elem.get("DisplayName", "") or "Transition",
            "condition": condition,
            "condition_refs": ctx.var_refs(condition, "read"),
        }
        if action_elem is not None:
            transition_info["action"] = _process_activity_node(action_elem, ctx)
        if to_name:
            target_elem = node_map.get(to_name)
            transition_info["target"] = (
                target_elem.get("DisplayName", "") or "State" if target_elem is not None else to_name
            )
            if to_name in visited:
                transition_info["loops_back"] = True
            else:
                # Recurse immediately (depth-first) so this target - and everything only
                # reachable through it - is emitted right after the current state, instead
                # of being deferred behind a sibling transition's own target (see docstring).
                nested_steps.extend(_walk_state_machine(to_name, node_map, ctx, visited))
        transitions_out.append(transition_info)

    if transitions_out:
        state_node["transitions"] = transitions_out

    return [state_node] + nested_steps


def extract_statemachine_steps(statemachine_elem: ET.Element, ctx: FlowCtx) -> list[dict]:
    """
    Reconstructs a StateMachine's states and transitions by walking its state graph from
    the initial state (see module note above _STATE_TAG for why this can't be a simple
    document-order pass).

    UiPath/.NET XAML writes the initial state in one of two shapes, and neither is safe to
    skip:
    - A plain XML attribute directly on <StateMachine>: InitialState="{x:Reference name}"
      (markup-extension syntax) - this is the common case Studio actually emits.
    - A <StateMachine.InitialState> property element wrapping an <x:Reference> or an inline
      node, mirroring Flowchart.StartNode's shape - seen in some other serializations.
    Document order is NOT a reliable fallback: under the nested x:Reference form (see
    module note above _FLOW_NODE_TAGS), the state that happens to sit as a direct child of
    <StateMachine> is just whichever one the serializer reached first while writing the
    graph - almost always NOT the actual initial state - so treating "first state found"
    as the default silently produces the wrong start state whenever InitialState isn't
    resolved. The bare "first in node_map" fallback below only fires if genuinely neither
    form is present or resolvable, and should be treated as a best-effort guess.
    """
    node_map = _build_state_node_map(statemachine_elem)
    if not node_map:
        return []

    start_name = _parse_initial_state_attr(statemachine_elem.get("InitialState") or "")

    if start_name is None:
        for child in statemachine_elem:
            if local_name(child.tag) == "StateMachine.InitialState":
                start_name = _resolve_flow_ref(child, node_map)
                break

    if start_name is None or start_name not in node_map:
        start_name = next(iter(node_map))

    visited: set[str] = set()
    steps = _walk_state_machine(start_name, node_map, ctx, visited)

    # Anything never reached from the initial state (dead/orphaned states) still deserves
    # to show up rather than silently vanishing from the map. visited is shared across
    # calls, so states reached by an earlier orphan's own transitions aren't re-emitted.
    for name in list(node_map):
        if name not in visited:
            steps.extend(_walk_state_machine(name, node_map, ctx, visited))

    return steps