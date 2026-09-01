"""
UiPath XAML Scanner - Step 2: Arguments & Variables

In addition to discovery and parsing (Step 1), this step extracts:
1. Arguments - from <x:Members> at the top of each file (name, direction, type)
2. Variables - from any <Something.Variables> in the file, including their scope
3. Full activity tree - every activity in the project, structured and nested exactly as it
   appears in the file (not just If/Switch) - see extract_activity_flow below
4. Scope-aware variable/argument reference tracking - every mention of a known variable or
   argument in a flow-relevant field (Assign, argument values, conditions) is resolved to a
   specific declaration (handling shadowing correctly via a lexical scope stack), so the
   frontend can let a developer click a variable and see everywhere it's used - see FlowCtx
   and _process_activity_node below.

This file holds ONLY the extraction/parsing logic - everything that isn't logic (picking a
project folder, finding .xaml files on disk) lives in project_io.py and is imported below.
The entry point that ties folder-picking + scanning + writing the JSON catalog together
lives in its own script, not here.

Graph-shaped root activities (Flowchart, StateMachine) are walked in graph_flow.py instead
of here - unlike Sequence/If/Switch/TryCatch, they aren't nested trees in the XML, so
reconstructing their visual execution order needs a different technique (graph walk from a
start node, with join-point/loop-back handling) than the plain recursive descent used for
everything else in this file. extract_activity_flow (below) imports from it lazily to avoid
a circular import, since graph_flow.py itself imports shared helpers and _process_activity_node
from this module.

Cross-file linking (InvokeWorkflowFile extraction + WorkflowFileName path resolution +
the reverse "invoked_by" index) lives in invocations.py instead of here, for the same
reason it's worth separating from per-activity extraction: it has no dependency on
FlowCtx/the scope stack at all, only on plain XML attributes and project file paths.
parse_xaml_file/build_catalog (below) import from it lazily, same circular-import-avoidance
pattern as graph_flow.py.

Top-to-bottom layout of this file:
  1. Namespace/tag constants
  2. Small shared helpers (local_name, node ids, annotations)
  3. Arguments & variables extraction (file-level: <x:Members>, <*.Variables>)
  4. Reference tracking (FlowCtx, var_refs) - resolves expression text to declarations
  5. Per-activity-type processors (Assign, If, IfElseIfV2, Switch, TryCatch, InvokeCode...)
  6. extract_activity_flow - entry point for the whole per-file activity tree (delegates to
     graph_flow.py for Flowchart/StateMachine roots)
  7. parse_xaml_file / build_catalog - orchestrates all of the above per file/project
     (delegates to invocations.py for cross-file linking)

See graph_flow.py for: Flowchart graph walking (FlowStep/FlowDecision/FlowSwitch) and
StateMachine graph walking (State/Transition).
See invocations.py for: InvokeWorkflowFile extraction, WorkflowFileName path resolution,
and the invoked_by dependency index.
"""

import itertools
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from project_io import find_xaml_files, pick_project_folder  # noqa: F401 (re-exported for callers of this module)
from activity_display import (
    ACTIVITY_SPECS,
    LoopSpec,
    select_key_arguments,
    humanize_keyboard_shortcuts,
    build_invoke_code_key_arguments,
    merge_send_hotkey_key_arguments,
    build_filter_data_table_key_arguments,
)

X_NS = "http://schemas.microsoft.com/winfx/2006/xaml"
ACTIVITIES_NS = "http://schemas.microsoft.com/netfx/2009/xaml/activities"
UI_NS = "http://schemas.uipath.com/workflow/activities"
SAP2010_NS = "http://schemas.microsoft.com/netfx/2010/xaml/activities/presentation"

INVOKE_WORKFLOW_TAG = f"{{{UI_NS}}}InvokeWorkflowFile"
ID_REF_ATTR = f"{{{SAP2010_NS}}}WorkflowViewState.IdRef"

_fallback_id_counter = itertools.count(1)

# Maps XAML type prefixes to readable argument directions
_ARGUMENT_DIRECTION_PREFIXES = {
    "InArgument": "In",
    "OutArgument": "Out",
    "InOutArgument": "In/Out",
}

# XAML's literal marker for "this argument exists but has no value set".
_NULL_MARKERS = {"{x:Null}", ""}


def local_name(tag: str) -> str:
    """Strips the namespace from a tag, e.g., '{http://...}Sequence' -> 'Sequence'."""
    return tag.split("}")[-1] if "}" in tag else tag


# Custom-library activities appear in XAML with a tag namespace of the form
# "clr-namespace:<Namespace>;assembly=<Assembly>" (see xmlns:o="clr-namespace:
# Odcanit_Library_New;assembly=Odcanit_Library_New" at the root of the file, then
# <o:SomeActivity> further down). Official UiPath activity packages (Mail, Excel, PDF,
# etc.) also load this way, but their assembly always starts with "UiPath." - as do the
# .NET/WF framework's own clr-namespace activities, which start with "System." or
# "Microsoft.". Anything else is a genuinely custom/in-house library, which is the only
# case we want to flag - not official UiPath packages.
_CLR_NAMESPACE_PREFIX = "clr-namespace:"
_BUILTIN_ASSEMBLY_PREFIXES = ("System.", "Microsoft.", "UiPath.")


def library_assembly_name(tag: str) -> str | None:
    """
    If `tag` is a namespaced activity tag (e.g. '{clr-namespace:Odcanit_Library_New;
    assembly=Odcanit_Library_New}Odcanit_Move_Cases_To_Other_Group') whose namespace is a
    clr-namespace backed by a custom/in-house assembly, returns that assembly's name.
    Returns None for built-in activities (System.*/Microsoft.*/UiPath.* assemblies, or any
    non-clr-namespace tag such as the core ACTIVITIES_NS/UI_NS activities).
    """
    if "}" not in tag:
        return None
    namespace_uri = tag[1:tag.index("}")]
    if not namespace_uri.startswith(_CLR_NAMESPACE_PREFIX):
        return None
    body = namespace_uri[len(_CLR_NAMESPACE_PREFIX):]  # "<Namespace>;assembly=<Assembly>"
    assembly = ""
    for part in body.split(";"):
        if part.startswith("assembly="):
            assembly = part[len("assembly="):]
            break
    if not assembly or assembly.startswith(_BUILTIN_ASSEMBLY_PREFIXES):
        return None
    return assembly


SAP2010_NS = "http://schemas.microsoft.com/netfx/2010/xaml/activities/presentation"
_ANNOTATION_ATTR = f"{{{SAP2010_NS}}}Annotation.AnnotationText"


def _get_annotation(elem: ET.Element) -> str | None:
    """
    Reads the developer's annotation/comment on an activity (the sticky-note text set via
    Studio's "Add Annotation", stored as sap2010:Annotation.AnnotationText) - distinct from
    DisplayName, which is the activity's title, not its explanatory comment.
    """
    text = (elem.get(_ANNOTATION_ATTR) or "").strip()
    return text or None


def _extract_condition_expression(container_elem: ET.Element | None) -> str:
    """
    Extracts condition/expression text from a property element, handling both forms
    UiPath emits: a simple InArgument/OutArgument wrapper with the expression as text
    content (the common case for e.g. <If Condition="...">'s equivalent property-element
    form), and a VisualBasicValue/VisualBasicReference/CSharpValue wrapper (the common
    case for FlowDecision.Condition, FlowSwitch.Expression, and
    InterruptibleDoWhile.Condition) whose expression lives in an ExpressionText attribute
    instead of the element's text. Used by LoopSpec.condition_property-driven extraction
    in _process_while_node, and mirrored by graph_flow.py's own copy for the same reason
    (Flowchart/StateMachine condition properties use the identical two shapes) - kept as
    two copies rather than one shared import to avoid a third module coupled into the
    scanner.py <-> graph_flow.py circular-import dance already documented at the top of
    this file; if a third caller ever needs this, worth revisiting.
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


def _get_node_id(elem: ET.Element, tag: str) -> str:
    """
    Returns a stable identifier for an activity node, used both to attach developer notes
    to it across re-scans, and as the "container id" component of variable/delegate
    declaration ids (see FlowCtx) so two same-named variables declared in two different
    containers never collide. Prefers UiPath Studio's own WorkflowViewState.IdRef (unique
    per activity, persists as long as the activity isn't deleted/regenerated in Studio).
    Falls back to a synthetic id for the rare case an element lacks it.
    """
    idref = elem.get(ID_REF_ATTR)
    if idref:
        return idref
    return f"{tag}_auto{next(_fallback_id_counter)}"


def parse_argument_type(type_str: str) -> dict:
    """
    Parses a type string like 'InArgument(x:String)' or
    'OutArgument(s:List(x:String))' into direction + inner type.
    """
    for prefix, direction in _ARGUMENT_DIRECTION_PREFIXES.items():
        if type_str.startswith(prefix + "("):
            inner_type = type_str[len(prefix) + 1 : -1]
            return {"direction": direction, "type": inner_type}

    # If it's not In/Out/InOutArgument, it's likely a standard x:Property
    return {"direction": "Property", "type": type_str}


def extract_arguments(root: ET.Element) -> list[dict]:
    """Extracts all arguments defined in <x:Members> at the top of the file."""
    members = root.find(f"{{{X_NS}}}Members")
    if members is None:
        return []

    arguments = []
    for prop in members.findall(f"{{{X_NS}}}Property"):
        parsed = parse_argument_type(prop.get("Type", ""))
        arguments.append(
            {
                "name": prop.get("Name", ""),
                "direction": parsed["direction"],
                "type": parsed["type"],
            }
        )
    return arguments


def extract_variables(root: ET.Element) -> list[dict]:
    """
    Extracts all variables in the file across all scopes (not just root), for the
    Variables panel in the UI. Saves the container defining each variable
    (Sequence/Flowchart/etc.) to preserve scope hierarchy instead of returning a flat list.
    This is independent of the scope-stack tracking used for reference resolution (see
    FlowCtx) - that one is built freshly during the flow walk, since it needs each
    declaration's container node id, which isn't available in this flatter pass.
    """
    variable_tag = f"{{{ACTIVITIES_NS}}}Variable"
    type_args_attr = f"{{{X_NS}}}TypeArguments"

    # Child-to-parent mapping since ElementTree doesn't provide direct parent access
    parent_map = {child: parent for parent in root.iter() for child in parent}

    variables = []
    for var_elem in root.iter(variable_tag):
        variables_container = parent_map.get(var_elem)  # e.g., Sequence.Variables
        owner = parent_map.get(variables_container) if variables_container is not None else None
        scope = local_name(owner.tag) if owner is not None else "root"

        variables.append(
            {
                "name": var_elem.get("Name", ""),
                "type": var_elem.get(type_args_attr, ""),
                "scope": scope,
            }
        )
    return variables


_DELEGATE_ARG_TAGS = {"DelegateInArgument", "DelegateOutArgument", "DelegateInOutArgument"}


def extract_scope_variable_names(root: ET.Element) -> set[str]:
    """
    Collects names of delegate arguments (ForEach loop items, TryCatch exception variables,
    etc) anywhere in the file. These behave like variables within their scope but aren't
    declared via <Variable> elements, so extract_variables() alone wouldn't find them.
    """
    names = set()
    for elem in root.iter():
        if local_name(elem.tag) in _DELEGATE_ARG_TAGS:
            name = elem.get("Name")
            if name:
                names.add(name)
    return names


def _build_variable_reference_matcher(variable_names: set[str]):
    """
    Compiles a regex matching any of the file's known variable/argument names as a whole
    identifier (word boundaries), so 'price' matches inside '[price + 1]' but doesn't
    false-positive on 'total_price'. Returns None when there's nothing to match against.
    Longer names are tried first so a name that's a prefix of another (e.g. 'price' vs
    'price2') can't shadow the longer match.
    """
    names = sorted({n for n in variable_names if n}, key=len, reverse=True)
    if not names:
        return None
    pattern = r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b"
    return re.compile(pattern)


def _expression_references_variable(expr: str, matcher) -> bool:
    """
    True if the expression text references a known variable/argument of the file - the
    signal that a field represents actual data flow, as opposed to an activity's static
    internal configuration (booleans, enums, timeouts, hardcoded literals).
    """
    if matcher is None:
        return False
    return bool(matcher.search(expr))


class FlowCtx:
    """
    Bundles the state threaded through activity-flow extraction:
    - matcher: regex that spots any known variable/argument name mentioned in free text.
    - scope_stack: lexical scope, innermost frame last. Each frame maps a name to the
      decl_id of the specific declaration it currently refers to at this point in the
      tree - resolving via this stack (rather than a flat global name lookup) is what
      makes shadowing (two same-named variables in two different containers) resolve
      correctly instead of getting confused with each other.
    - declarations: file-wide dict of decl_id -> {name, type, scope, direction}, used by
      the frontend to show full context (type/scope/direction) when a developer traces a
      variable, without needing to re-derive it from the tree at click time.
    """

    __slots__ = ("matcher", "scope_stack", "declarations")

    def __init__(self, matcher, scope_stack: list[dict], declarations: dict):
        self.matcher = matcher
        self.scope_stack = scope_stack
        self.declarations = declarations

    def resolve(self, name: str) -> str | None:
        for frame in reversed(self.scope_stack):
            if name in frame:
                return frame[name]
        return None

    def push(self, frame: dict) -> None:
        self.scope_stack.append(frame)

    def pop(self) -> None:
        self.scope_stack.pop()

    def var_refs(self, text: str | None, role: str) -> list[dict]:
        """
        Finds every mention of a known variable/argument in `text` and resolves each to its
        specific declaration given the CURRENT scope stack. Deduplicated by decl_id (a name
        used twice in one expression, e.g. 'x + x', only needs one clickable reference).
        Mentions that can't be resolved against the current scope (e.g. a name that's
        declared only in a sibling branch, not actually in scope here) are silently
        dropped rather than guessed at - better to under-link than to link incorrectly.
        """
        if not text or self.matcher is None:
            return []
        found: dict[str, dict] = {}
        for m in self.matcher.finditer(text):
            name = m.group(0)
            decl_id = self.resolve(name)
            if decl_id and decl_id not in found:
                found[decl_id] = {"name": name, "decl_id": decl_id, "role": role}
        return list(found.values())

    def register_declarations(self, frame_info: dict, scope_label: str) -> dict:
        """
        Registers each (name -> {decl_id, type}) entry in frame_info into the file-wide
        declarations dict (first registration wins - a decl_id is unique per container so
        this only ever fires once per declaration), and returns the scope_stack frame
        (name -> decl_id) ready to push.
        """
        frame = {}
        for name, info in frame_info.items():
            self.declarations.setdefault(info["decl_id"], {
                "name": name,
                "type": info.get("type", ""),
                "scope": scope_label,
                "direction": info.get("direction"),
            })
            frame[name] = info["decl_id"]
        return frame


def extract_filter_data_table_conditions(filter_elem: ET.Element) -> list[dict]:
    """
    Extracts FilterDataTable's actual filter conditions from
    <ui:FilterDataTable.Filters><scg:List ...><ui:FilterOperationArgument ...>...
    </scg:List></ui:FilterDataTable.Filters> - the part of the activity that says WHICH
    rows get kept/removed, as opposed to DataTable/OutputDataTable (which only say what
    table goes in and what comes out). Each FilterOperationArgument holds a Column and
    an Operand as property elements (FilterOperationArgument.Column/.Operand, each
    wrapping an InArgument the same way every other property-element argument in this
    codebase does), plus BooleanOperator ("And"/"Or", how this condition combines with
    the one before it) and Operator (e.g. "CONTAINS", "=") as plain XML attributes.

    Returns one dict per condition, in document order, with column/operator/operand/
    boolean_operator - display-side row shape (compact flag, combining into readable
    text) is activity_display.py's job (see build_filter_data_table_key_arguments),
    same division of responsibility as extract_invoke_code_arguments/
    build_invoke_code_key_arguments.
    """
    filters_container = filter_elem.find(f"{{{UI_NS}}}FilterDataTable.Filters")
    if filters_container is None:
        return []

    conditions = []
    for cond_elem in filters_container.iter(f"{{{UI_NS}}}FilterOperationArgument"):
        column_elem = cond_elem.find(f"{{{UI_NS}}}FilterOperationArgument.Column")
        operand_elem = cond_elem.find(f"{{{UI_NS}}}FilterOperationArgument.Operand")
        conditions.append(
            {
                "column": _first_argument_text(column_elem) or "",
                "operator": cond_elem.get("Operator", ""),
                "operand": _first_argument_text(operand_elem) or "",
                "boolean_operator": cond_elem.get("BooleanOperator", ""),
            }
        )
    return conditions


def extract_invoke_code_arguments(invoke_code_elem: ET.Element) -> list[dict]:
    """
    Extracts an InvokeCode activity's own parameters from <ui:InvokeCode.Arguments> - each
    an InArgument/OutArgument/InOutArgument keyed (via x:Key) by the variable name used
    *inside* the Code block, bound to an expression evaluated in the *outer* scope (e.g.
    <InArgument x:Key="a">[aaa]</InArgument> means the code's local "a" receives "aaa").
    Structurally identical to InvokeWorkflowFile's argument bindings (see
    extract_argument_bindings) but lives under a different property element.
    """
    key_attr = f"{{{X_NS}}}Key"
    type_args_attr = f"{{{X_NS}}}TypeArguments"

    container = invoke_code_elem.find(f"{{{UI_NS}}}InvokeCode.Arguments")
    if container is None:
        return []

    bindings = []
    for child in container.iter():
        if child is container:
            continue
        tag_name = local_name(child.tag)  # InArgument / OutArgument / InOutArgument
        if tag_name not in _ARGUMENT_DIRECTION_PREFIXES:
            # Skip wrapper elements (e.g. an empty scg:Dictionary representing zero
            # arguments) - see extract_argument_bindings for the full explanation.
            continue
        direction = _ARGUMENT_DIRECTION_PREFIXES[tag_name]
        bindings.append(
            {
                "name": child.get(key_attr, ""),
                "direction": direction,
                "type": child.get(type_args_attr, ""),
                "expression": (child.text or "").strip(),
            }
        )
    return bindings


def _first_argument_text(container_elem: ET.Element | None) -> str | None:
    """
    Given an Assign.To / Assign.Value wrapper element, returns the inner argument's
    expression text (e.g. the OutArgument/InArgument's own text content).
    """
    if container_elem is None:
        return None
    for child in container_elem:
        return (child.text or "").strip()
    return (container_elem.text or "").strip() or None


def _extract_assign_pairs(assign_elem: ET.Element) -> list[dict]:
    """
    Extracts the To/Value expression pair from a classic <Assign> activity, so the flow
    view can show what the assignment actually does instead of just the generic activity
    name "Assign". Reference resolution (to_refs/value_refs) is added by the caller, which
    has access to the current FlowCtx.
    """
    to_elem = None
    value_elem = None
    for child in assign_elem:
        tag = local_name(child.tag)
        if tag == "Assign.To":
            to_elem = child
        elif tag == "Assign.Value":
            value_elem = child

    to_expr = _first_argument_text(to_elem)
    value_expr = _first_argument_text(value_elem)
    if to_expr is None and value_expr is None:
        return []
    return [{"to": to_expr or "?", "value": value_expr or "?"}]


def _extract_multiple_assign_pairs(multi_assign_elem: ET.Element) -> list[dict]:
    """
    Extracts To/Value pairs from a 'MultipleAssign' activity. Structure:
    <MultipleAssign.AssignOperations>
      <scg:List x:TypeArguments="ui:AssignOperation">
        <AssignOperation>
          <AssignOperation.To><OutArgument>...</OutArgument></AssignOperation.To>
          <AssignOperation.Value><InArgument>...</InArgument></AssignOperation.Value>
        </AssignOperation>
        ...
      </scg:List>
    </MultipleAssign.AssignOperations>

    Uses .iter() rather than direct children, since AssignOperation sits inside an extra
    scg:List wrapper.
    """
    container = None
    for child in multi_assign_elem:
        if local_name(child.tag) == "MultipleAssign.AssignOperations":
            container = child
            break
    if container is None:
        return []

    assignments = []
    for op in container.iter():
        if local_name(op.tag) != "AssignOperation":
            continue
        to_elem = None
        value_elem = None
        for sub in op:
            sub_tag = local_name(sub.tag)
            if sub_tag.endswith(".To"):
                to_elem = sub
            elif sub_tag.endswith(".Value"):
                value_elem = sub
        to_expr = _first_argument_text(to_elem)
        value_expr = _first_argument_text(value_elem)
        if to_expr is not None or value_expr is not None:
            assignments.append({"to": to_expr or "?", "value": value_expr or "?"})

    return assignments


# Tags representing branching logic in the flow (represented as explicit branches)
_BRANCHING_TAGS = {"If", "Switch", "TryCatch"}

# Property elements containing executable content (unwrapped in generic flow processing).
# Any other property element containing a dot (Variables, ViewState, Condition, etc.) is metadata.
_CONTAINER_BODY_SUFFIXES = {"Body", "ActivityBody"}


def _get_delegate_argument_name(activity_action_elem: ET.Element) -> str | None:
    """
    Extracts the DelegateInArgument (or Out/InOut) name from <ActivityAction.Argument>.
    This is the container's scoped variable (e.g., 'item' in ForEach, 'exception' in Catch).
    """
    for child in activity_action_elem:
        if local_name(child.tag) == "ActivityAction.Argument":
            for grandchild in child:
                if local_name(grandchild.tag).startswith("Delegate"):
                    return grandchild.get("Name", "")
    return None


def _get_delegate_argument_info(activity_action_elem: ET.Element) -> tuple[str | None, str | None]:
    """Like _get_delegate_argument_name, but also returns the declared type (x:TypeArguments)."""
    type_args_attr = f"{{{X_NS}}}TypeArguments"
    for child in activity_action_elem:
        if local_name(child.tag) == "ActivityAction.Argument":
            for grandchild in child:
                if local_name(grandchild.tag).startswith("Delegate"):
                    return grandchild.get("Name", ""), grandchild.get(type_args_attr, "")
    return None, None


def _get_real_children(container_elem: ET.Element) -> list[tuple[ET.Element, str | None]]:
    """
    Returns actual child activities of a container as (element, scope_variable_or_None) pairs,
    transparently unwrapping XAML structural plumbing:
    - Content property elements (X.Body / X.ActivityBody) are recursively unwrapped.
    - ActivityAction wrappers are unwrapped, saving DelegateInArgument as scope_variable.
    - Other property elements (Variables, ViewState, Condition, Arguments) are skipped.
    """
    real_children: list[tuple[ET.Element, str | None]] = []

    for child in container_elem:
        tag = local_name(child.tag)

        if "." in tag:
            suffix = tag.split(".", 1)[1]
            if suffix in _CONTAINER_BODY_SUFFIXES:
                real_children.extend(_get_real_children(child))
            continue

        if tag == "ActivityAction":
            scope_variable = _get_delegate_argument_name(child)
            for inner in child:
                if "." in local_name(inner.tag):
                    continue  # ActivityAction.Argument already extracted
                real_children.append((inner, scope_variable))
            continue

        real_children.append((child, None))

    return real_children


def _extract_direct_variable_decls(elem: ET.Element) -> dict:
    """
    Returns the variables declared directly in this element's own <Tag.Variables> clause
    (not recursing into children) as {name: {"type": ...}}. Used by _process_activity_node
    to push a new scope frame right before this element's own children are resolved.
    """
    decls = {}
    type_args_attr = f"{{{X_NS}}}TypeArguments"
    for child in elem:
        tag = local_name(child.tag)
        if tag.endswith(".Variables"):
            for var_elem in child:
                if local_name(var_elem.tag) == "Variable":
                    name = var_elem.get("Name", "")
                    if name:
                        decls[name] = {"type": var_elem.get(type_args_attr, "")}
    return decls


def _extract_direct_delegate_decl(elem: ET.Element) -> dict:
    """
    Returns the delegate variable declared directly in this element's own body (e.g. a
    ForEach's loop item, a Catch's exception variable) as {name: {"type": ...}}, without
    recursing further than necessary to find it.
    """
    for child in elem:
        tag = local_name(child.tag)
        if "." in tag and tag.split(".", 1)[1] in _CONTAINER_BODY_SUFFIXES:
            for grandchild in child:
                if local_name(grandchild.tag) == "ActivityAction":
                    name, vtype = _get_delegate_argument_info(grandchild)
                    if name:
                        return {name: {"type": vtype or ""}}
    return {}


def _extract_activity_raw_arguments(elem: ET.Element, tag: str) -> list[dict]:
    """
    Extracts every property-element and plain-attribute argument on an activity, dropping
    only null/empty values. Property-element arguments also capture their direction
    (In/Out/In-Out) from the wrapping InArgument/OutArgument/InOutArgument tag, used both
    to pick a read/write/readwrite role for reference tracking and by the generic
    variable-reference fallback filter.
    """
    args: list[dict] = []
    seen_names: set[str] = set()

    prefix = f"{tag}."
    for child in elem:
        child_tag = local_name(child.tag)
        if not child_tag.startswith(prefix):
            continue
        prop_name = child_tag[len(prefix):]
        seen_names.add(prop_name)

        direction = None
        expr = None
        wrapper = next(iter(child), None)
        if wrapper is not None:
            direction = _ARGUMENT_DIRECTION_PREFIXES.get(local_name(wrapper.tag))
            expr = (wrapper.text or "").strip()
        else:
            expr = (child.text or "").strip() or None

        if expr is not None and expr not in _NULL_MARKERS:
            args.append({"name": prop_name, "value": expr, "direction": direction})

    skip_attrs = {"DisplayName"}
    for attr_name, attr_value in elem.attrib.items():
        local_attr = local_name(attr_name)
        if local_attr in seen_names or local_attr in skip_attrs:
            continue
        if attr_name.startswith("{"):
            continue
        if attr_value and attr_value not in _NULL_MARKERS:
            args.append({"name": local_attr, "value": attr_value, "direction": None})

    return args


def _filter_variable_referencing(args: list[dict], matcher) -> list[dict]:
    """Keeps only arguments whose value references a known variable/argument."""
    return [a for a in args if _expression_references_variable(a["value"], matcher)]


def _role_for_direction(direction: str | None) -> str:
    if direction == "Out":
        return "write"
    if direction == "In/Out":
        return "readwrite"
    return "read"


def _process_steps(items: list[tuple[ET.Element, str | None]], ctx: FlowCtx) -> list[dict]:
    """Converts a list of (element, scope_variable) pairs into flow tree nodes."""
    steps = []
    for elem, scope_variable in items:
        node = _process_activity_node(elem, ctx)
        if scope_variable:
            node["scope_variable"] = scope_variable
        steps.append(node)
    return steps


def _process_activity_node(elem: ET.Element, ctx: FlowCtx) -> dict:
    """
    Converts a single XAML element into a flow tree node. Before descending into this
    element's own children, pushes a new scope frame for any variables/delegate argument
    this element itself declares (structural check - works for any activity type, not just
    ones we've hardcoded, since Variables/ActivityAction detection is tag-agnostic), pops
    it again once this node (including its children) is fully built.
    """
    tag = local_name(elem.tag)
    if tag == "If":
        return _process_if_node(elem, ctx)
    if tag == "IfElseIfV2":
        return _process_ifelseifv2_node(elem, ctx)
    if tag == "Switch":
        return _process_switch_node(elem, ctx)
    if tag == "TryCatch":
        return _process_trycatch_node(elem, ctx)
    if tag == "NCheckState":
        return _process_checkstate_node(elem, ctx)
    spec = ACTIVITY_SPECS.get(tag)
    if spec is not None and spec.loop is not None:
        return _process_while_node(elem, ctx, spec.loop)

    node_id = _get_node_id(elem, tag)
    scope_label = elem.get("DisplayName", "") or tag

    direct_decls = _extract_direct_variable_decls(elem)
    delegate_decls = _extract_direct_delegate_decl(elem)
    raw_frame_info = {}
    for name, info in {**direct_decls, **delegate_decls}.items():
        raw_frame_info[name] = {"decl_id": f"var::{node_id}::{name}", "type": info["type"]}
    frame = ctx.register_declarations(raw_frame_info, scope_label) if raw_frame_info else {}
    if frame:
        ctx.push(frame)

    try:
        node = {
            "type": "activity",
            "name": elem.get("DisplayName", "") or tag,
            "tag": tag,
            "id": node_id,
            "children": _process_steps(_get_real_children(elem), ctx),
        }

        library_assembly = library_assembly_name(elem.tag)
        if library_assembly:
            node["library_assembly"] = library_assembly

        annotation = _get_annotation(elem)
        if annotation:
            node["annotation"] = annotation

        if tag == "InvokeWorkflowFile":
            raw_path = elem.get("WorkflowFileName", "")
            node["raw_path"] = raw_path
            node["is_dynamic"] = raw_path.strip().startswith("[") or not raw_path
            # Attach argument bindings here too (not just via a separate root.iter() pass
            # in invocations.extract_invocations) so a file's ordered "invokes" list can be
            # derived directly from activity_flow - see collect_invocations_in_flow_order in
            # invocations.py - and therefore inherits activity_flow's already-correct
            # visual/logical order (document order for Sequence/If/Switch, graph-walk order
            # for Flowchart/StateMachine) instead of root.iter()'s raw document order, which
            # for a graph-shaped root can diverge from what a developer actually sees in
            # Studio (see collect_invocations_in_flow_order's docstring for a concrete
            # example: a REFramework Main.xaml's StateMachine).
            from invocations import extract_argument_bindings
            node["argument_bindings"] = extract_argument_bindings(elem)
        elif tag == "Assign":
            assignments = _extract_assign_pairs(elem)
            if assignments:
                for a in assignments:
                    a["to_refs"] = ctx.var_refs(a["to"], "write")
                    a["value_refs"] = ctx.var_refs(a["value"], "read")
                node["assignments"] = assignments
        elif tag == "MultipleAssign":
            assignments = _extract_multiple_assign_pairs(elem)
            if assignments:
                for a in assignments:
                    a["to_refs"] = ctx.var_refs(a["to"], "write")
                    a["value_refs"] = ctx.var_refs(a["value"], "read")
                node["assignments"] = assignments

        raw_args = _extract_activity_raw_arguments(elem, tag)
        for a in raw_args:
            a["value_refs"] = ctx.var_refs(a["value"], _role_for_direction(a.get("direction")))

        key_args = select_key_arguments(raw_args, tag)
        if key_args is not None:
            if tag == "NKeyboardShortcuts":
                for a in key_args:
                    if a["name"] == "Shortcuts" and a["value"]:
                        a["raw_value"] = a["value"]
                        a["value"] = humanize_keyboard_shortcuts(a["value"])
            elif tag == "SendHotkey":
                key_args = merge_send_hotkey_key_arguments(key_args)
            elif tag == "FilterDataTable":
                # Filters (the actual filter conditions) isn't an ordinary attribute -
                # it's a multi-entry property element - so it can't come from
                # select_key_arguments/ACTIVITY_SPECS the way DataTable/OutputDataTable
                # just did above; extracted and shaped separately, then appended onto
                # the same key_arguments list so all three render as one consistent set
                # of rows. See build_filter_data_table_key_arguments's own docstring for
                # the row format and why each needs its column/operand resolved into
                # value_refs here (rather than in activity_display.py, which has no
                # FlowCtx to do that with).
                conditions = extract_filter_data_table_conditions(elem)
                filter_rows = build_filter_data_table_key_arguments(conditions)
                for row in filter_rows:
                    column_expr, operand_expr = row.pop("_refs_source")
                    row["value_refs"] = ctx.var_refs(column_expr, "read") + ctx.var_refs(operand_expr, "read")
                key_args = key_args + filter_rows
            node["key_arguments"] = key_args

        generic_args = _filter_variable_referencing(raw_args, ctx.matcher)
        if generic_args:
            node["arguments"] = generic_args

        if tag == "InvokeCode":
            # Row shape (which fields, "compact": True) is defined once in
            # activity_display.py's build_invoke_code_key_arguments - see its own
            # docstring for why InvokeCode can't just use an ordinary ACTIVITY_SPECS
            # entry. This call site only does what that function deliberately can't:
            # extracting the raw pieces from the live ET.Element (Code attribute,
            # <ui:InvokeCode.Arguments> bindings) and computing each row's "value_refs"
            # via ctx.var_refs, both of which need FlowCtx/ET.Element that
            # activity_display.py has no dependency on.
            code_text = elem.get("Code", "") or ""
            bindings = extract_invoke_code_arguments(elem)
            combined = build_invoke_code_key_arguments(code_text, bindings)
            # combined's rows line up with (code_text's own row, if present) followed by
            # bindings in order - build_invoke_code_key_arguments only emits a "Code" row
            # when code_text actually has content, so that's the one case row 0 isn't a
            # binding.
            row_index = 0
            if combined and combined[0]["name"] == "Code":
                combined[0]["value_refs"] = ctx.var_refs(code_text, "read")
                row_index = 1
            for binding in bindings:
                combined[row_index]["value_refs"] = ctx.var_refs(
                    binding["expression"], _role_for_direction(binding["direction"])
                )
                row_index += 1
            if combined:
                node["key_arguments"] = combined

        return node
    finally:
        if frame:
            ctx.pop()


def _process_if_node(if_elem: ET.Element, ctx: FlowCtx) -> dict:
    """
    Converts <If> to a branch node. Else-If chains (nested single If inside If.Else)
    are flattened into a single list of branches (Then / Else If / ... / Else).
    """
    condition = (if_elem.get("Condition") or "").strip()
    then_elem = if_elem.find(f"{{{ACTIVITIES_NS}}}If.Then")
    else_elem = if_elem.find(f"{{{ACTIVITIES_NS}}}If.Else")

    branches = [
        {
            "label": "Then",
            "condition": None,
            "condition_refs": [],
            "steps": _process_steps(_get_real_children(then_elem), ctx) if then_elem is not None else [],
        }
    ]

    current_else = else_elem
    while current_else is not None:
        else_children = _get_real_children(current_else)
        if len(else_children) == 1 and local_name(else_children[0][0].tag) == "If":
            nested_if = else_children[0][0]
            nested_condition = (nested_if.get("Condition") or "").strip()
            nested_then = nested_if.find(f"{{{ACTIVITIES_NS}}}If.Then")
            branches.append(
                {
                    "label": "Else If",
                    "condition": nested_condition,
                    "condition_refs": ctx.var_refs(nested_condition, "read"),
                    "steps": _process_steps(_get_real_children(nested_then), ctx) if nested_then is not None else [],
                }
            )
            current_else = nested_if.find(f"{{{ACTIVITIES_NS}}}If.Else")
        else:
            branches.append(
                {
                    "label": "Else", "condition": None, "condition_refs": [],
                    "steps": _process_steps(else_children, ctx),
                }
            )
            current_else = None

    return {
        "type": "branch", "kind": "if", "id": _get_node_id(if_elem, "If"),
        "condition": condition, "condition_refs": ctx.var_refs(condition, "read"),
        "branches": branches,
        **({"annotation": _get_annotation(if_elem)} if _get_annotation(if_elem) else {}),
    }


def _process_while_node(elem: ET.Element, ctx: FlowCtx, loop_spec: LoopSpec) -> dict:
    """
    Converts a loop-shaped activity (any tag registered in ACTIVITY_SPECS with `loop`
    set - e.g. While, DoWhile, InterruptibleDoWhile) to a branch node with a single
    "Body" branch, mirroring _process_if_node's shape so the frontend renders it with
    the same decision-diamond-style header and Condition-in-the-header treatment as
    If/Switch - Condition is exactly the "structural/logic understanding" a loop's
    header should convey, the same reasoning _filter_steps in report.py already applies
    to If/Switch's own condition.

    Where the condition actually lives in XAML varies by tag - loop_spec (the calling
    tag's own LoopSpec, looked up once by the caller) says which of the two shapes to
    read: a plain attribute (condition_attr, e.g. While/DoWhile's Condition="...") or a
    property element (condition_property, e.g. InterruptibleDoWhile.Condition wrapping a
    VisualBasicValue/ExpressionText) - see LoopSpec's own docstring for why both exist.

    Beyond the condition, a loop activity also carries loop parameters (MaxIterations,
    Index/CurrentIndex depending on tag) - ordinary attributes, not part of the condition
    itself - which are surfaced as key_arguments on the branch node (rendered as argument
    rows under the branch header, the same way a container activity's key_arguments
    render) per this tag's ActivitySpec.fields. This still requires the direct/delegate
    variable-scope push _process_activity_node performs for every activity (a loop can
    declare its own <Tag.Variables>), so that dance is duplicated here rather than
    reusing _process_activity_node wholesale, since this function returns a "branch"
    node shape instead of the "activity" shape _process_activity_node builds.
    """
    tag = local_name(elem.tag)
    node_id = _get_node_id(elem, tag)
    scope_label = elem.get("DisplayName", "") or tag

    direct_decls = _extract_direct_variable_decls(elem)
    delegate_decls = _extract_direct_delegate_decl(elem)
    raw_frame_info = {}
    for name, info in {**direct_decls, **delegate_decls}.items():
        raw_frame_info[name] = {"decl_id": f"var::{node_id}::{name}", "type": info["type"]}
    frame = ctx.register_declarations(raw_frame_info, scope_label) if raw_frame_info else {}
    if frame:
        ctx.push(frame)

    try:
        if loop_spec.condition_property:
            # Property elements follow the activity's OWN tag namespace (e.g.
            # ui:InterruptibleDoWhile.Condition lives in UI_NS, not the core
            # ACTIVITIES_NS that <While>/<DoWhile> themselves live in) - elem.tag already
            # carries that correct namespace URI, so reuse it directly rather than
            # assuming ACTIVITIES_NS the way _process_if_node can for the built-in If.
            tag_ns = elem.tag[1:elem.tag.index("}")] if elem.tag.startswith("{") else ""
            prop_elem = elem.find(f"{{{tag_ns}}}{tag}.{loop_spec.condition_property}") if tag_ns else \
                elem.find(f"{tag}.{loop_spec.condition_property}")
            if prop_elem is not None:
                condition = _extract_condition_expression(prop_elem)
            else:
                # Confirmed against a real project: InterruptibleDoWhile can ALSO write
                # its Condition as a plain XML attribute directly on the element (e.g.
                # Condition="[1 = 1]") rather than as the InterruptibleDoWhile.Condition
                # property element LoopSpec.condition_property was written for -
                # apparently both shapes occur for this same tag (possibly a version
                # difference in how Studio serializes it), not just across different
                # tags the way While/DoWhile vs. InterruptibleDoWhile originally
                # motivated condition_attr vs. condition_property as separate options.
                # Falling back to the plain-attribute reading here (using the same
                # attribute name, "Condition", condition_property was registered with)
                # means a single LoopSpec entry now tolerates either shape, instead of
                # requiring the registration to guess which one a given project uses.
                condition = (elem.get(loop_spec.condition_property) or "").strip()
        else:
            condition = (elem.get(loop_spec.condition_attr or "Condition") or "").strip()

        raw_args = _extract_activity_raw_arguments(elem, tag)
        for a in raw_args:
            a["value_refs"] = ctx.var_refs(a["value"], _role_for_direction(a.get("direction")))
        key_args = select_key_arguments(raw_args, tag) or []

        body_steps = _process_steps(_get_real_children(elem), ctx)

        node = {
            "type": "branch", "kind": "while", "id": node_id,
            "name": elem.get("DisplayName", "") or tag,
            "condition": condition, "condition_refs": ctx.var_refs(condition, "read"),
            "branches": [
                {"label": "Body", "condition": None, "condition_refs": [], "steps": body_steps},
            ],
        }
        if key_args:
            node["key_arguments"] = key_args
        annotation = _get_annotation(elem)
        if annotation:
            node["annotation"] = annotation
        return node
    finally:
        if frame:
            ctx.pop()


def _process_ifelseifv2_node(elem: ET.Element, ctx: FlowCtx) -> dict:
    """
    Converts UiPath's modern <ui:IfElseIfV2> (the current "If" activity, with built-in
    Else-If support) into the same Then / Else If / ... / Else branch shape
    _process_if_node produces for the classic <If>, so the flow view renders it
    identically - no frontend changes needed. Structurally different from classic If
    though: Condition is a plain attribute here (not an InArgument-wrapped property
    element), and each Else-If is its own <ui:IfElseIfBlock> (also a plain Condition
    attribute) living inside <ui:IfElseIfV2.ElseIfs><sc:BindingList>...</sc:BindingList>,
    rather than being expressed as a nested <If.Else><If>... chain.
    """
    condition = (elem.get("Condition") or "").strip()

    then_elem = elem.find(f"{{{UI_NS}}}IfElseIfV2.Then")
    branches = [
        {
            "label": "Then",
            "condition": None,
            "condition_refs": [],
            "steps": _process_steps(_get_real_children(then_elem), ctx) if then_elem is not None else [],
        }
    ]

    elseifs_elem = elem.find(f"{{{UI_NS}}}IfElseIfV2.ElseIfs")
    if elseifs_elem is not None:
        # The BindingList wrapper's own namespace/prefix (sc:BindingList) doesn't matter -
        # every IfElseIfBlock inside it, at any depth, is one Else-If branch, in document
        # order (which is also visual/execution order for this activity).
        for block in elseifs_elem.iter():
            if local_name(block.tag) != "IfElseIfBlock":
                continue
            block_condition = (block.get("Condition") or "").strip()
            block_then = block.find(f"{{{UI_NS}}}IfElseIfBlock.Then")
            branches.append(
                {
                    "label": "Else If",
                    "condition": block_condition,
                    "condition_refs": ctx.var_refs(block_condition, "read"),
                    "steps": _process_steps(_get_real_children(block_then), ctx) if block_then is not None else [],
                }
            )

    else_elem = elem.find(f"{{{UI_NS}}}IfElseIfV2.Else")
    if else_elem is not None:
        else_children = _get_real_children(else_elem)
        if else_children:
            branches.append(
                {
                    "label": "Else",
                    "condition": None,
                    "condition_refs": [],
                    "steps": _process_steps(else_children, ctx),
                }
            )

    return {
        "type": "branch", "kind": "if", "id": _get_node_id(elem, "IfElseIfV2"),
        "condition": condition, "condition_refs": ctx.var_refs(condition, "read"),
        "branches": branches,
        **({"annotation": _get_annotation(elem)} if _get_annotation(elem) else {}),
    }


def _process_switch_node(switch_elem: ET.Element, ctx: FlowCtx) -> dict:
    """
    Converts <Switch> to a branch node. Each case is a typed element with x:Key,
    and Switch.Default serves as default.
    """
    key_attr = f"{{{X_NS}}}Key"
    condition = (switch_elem.get("Expression") or "").strip()

    branches = []
    for child in switch_elem:
        tag = local_name(child.tag)
        label = "Default" if tag == "Switch.Default" else f"Case {child.get(key_attr, '?')}"
        branches.append(
            {
                "label": label, "condition": None, "condition_refs": [],
                "steps": _process_steps(_get_real_children(child), ctx),
            }
        )

    return {
        "type": "branch", "kind": "switch", "id": _get_node_id(switch_elem, "Switch"),
        "condition": condition, "condition_refs": ctx.var_refs(condition, "read"),
        "branches": branches,
        **({"annotation": _get_annotation(switch_elem)} if _get_annotation(switch_elem) else {}),
    }


def _process_trycatch_node(trycatch_elem: ET.Element, ctx: FlowCtx) -> dict:
    """
    Converts <TryCatch> to a branch node with up to three branch types:
    Try, Catch (per defined exception type), and Finally (if non-empty).
    """
    branches = []

    try_elem = trycatch_elem.find(f"{{{ACTIVITIES_NS}}}TryCatch.Try")
    if try_elem is not None:
        branches.append(
            {
                "label": "Try", "condition": None, "condition_refs": [],
                "steps": _process_steps(_get_real_children(try_elem), ctx),
            }
        )

    catches_elem = trycatch_elem.find(f"{{{ACTIVITIES_NS}}}TryCatch.Catches")
    if catches_elem is not None:
        type_args_attr = f"{{{X_NS}}}TypeArguments"
        for catch_elem in catches_elem.findall(f"{{{ACTIVITIES_NS}}}Catch"):
            exception_type = catch_elem.get(type_args_attr, "")
            label = f"Catch ({exception_type})" if exception_type else "Catch"

            # The Catch's own exception variable is declared at the Catch element level
            # itself, but _process_activity_node is only invoked per real child, never on
            # catch_elem directly - so its delegate scope must be pushed explicitly here,
            # mirroring the generic per-node mechanism in _process_activity_node.
            catch_id = _get_node_id(catch_elem, "Catch")
            delegate_decls = _extract_direct_delegate_decl(catch_elem)
            raw_frame_info = {
                name: {"decl_id": f"var::{catch_id}::{name}", "type": info["type"]}
                for name, info in delegate_decls.items()
            }
            frame = ctx.register_declarations(raw_frame_info, label) if raw_frame_info else {}
            if frame:
                ctx.push(frame)
            try:
                catch_annotation = _get_annotation(catch_elem)
                branches.append(
                    {
                        "label": label, "condition": None, "condition_refs": [],
                        "steps": _process_steps(_get_real_children(catch_elem), ctx),
                        **({"annotation": catch_annotation} if catch_annotation else {}),
                    }
                )
            finally:
                if frame:
                    ctx.pop()

    finally_elem = trycatch_elem.find(f"{{{ACTIVITIES_NS}}}TryCatch.Finally")
    if finally_elem is not None:
        finally_steps = _process_steps(_get_real_children(finally_elem), ctx)
        has_real_content = any(
            step.get("children") or step.get("branches") for step in finally_steps
        )
        if has_real_content:
            branches.append({"label": "Finally", "condition": None, "condition_refs": [], "steps": finally_steps})

    return {
        "type": "branch", "kind": "trycatch", "id": _get_node_id(trycatch_elem, "TryCatch"),
        "condition": None, "condition_refs": [], "branches": branches,
        **({"annotation": _get_annotation(trycatch_elem)} if _get_annotation(trycatch_elem) else {}),
    }


def _extract_checkstate_target_text(check_state_elem: ET.Element) -> str:
    """
    Returns a human-readable description of the UI element an NCheckState activity tests
    for presence, pulled from its <uix:NCheckState.Target> anchorable. Like Click/TypeInto's
    own Target, this property element has no text content of its own (only nested selector
    attributes on the wrapped TargetAnchorable), so the generic raw-argument extraction -
    built for simple text values - silently finds nothing here, which is why NCheckState
    otherwise renders with no visible condition at all. FullSelectorArgument (the selector
    actually evaluated at runtime, which can itself embed variable references via
    string.Format) is preferred; FuzzySelectorArgument is used as a fallback for anchorables
    that only carry a fuzzy/best-effort selector. Returns "" if no Target is present.
    """
    for child in check_state_elem:
        if local_name(child.tag).endswith(".Target"):
            anchorable = next(iter(child), None)
            if anchorable is None:
                return ""
            return anchorable.get("FullSelectorArgument") or anchorable.get("FuzzySelectorArgument") or ""
    return ""


def _process_checkstate_node(elem: ET.Element, ctx: FlowCtx) -> dict:
    """
    Converts UiPath Studio's custom <uix:NCheckState> activity into the same branch-node
    shape _process_if_node produces (kind="if", with Then/Else-style branches), so the flow
    view renders its structure identically to a real If with no frontend changes needed.
    Unlike a real If (which Studio never gives a meaningful DisplayName - it's always just
    "If"), NCheckState nodes are commonly renamed by the developer to describe what's being
    checked (e.g. "Check App State 'תוכן הפופאפ'"), so - unlike _process_if_node - an
    explicit "name" is included here to preserve that title rather than letting it default
    to whatever generic label the UI shows for kind="if".

    NCheckState tests whether its own <uix:NCheckState.Target> UI element currently exists
    on screen, then runs one of two fixed bodies: <uix:NCheckState.IfExists> or
    <uix:NCheckState.IfNotExists>. Both are property elements whose suffix isn't ".Body"/
    ".ActivityBody", so _get_real_children (used by the generic activity path) skips them
    entirely - which, combined with the Target having no extractable text (see
    _extract_checkstate_target_text), is why this activity previously rendered as a bare
    title with none of its actual behavior visible.

    There's no boolean expression attribute here the way a real If has one, so
    _extract_checkstate_target_text's selector description is used as a human-readable
    stand-in "condition" - it's run through ctx.var_refs the same way a real condition
    would be, since selectors occasionally embed variable references.
    """
    target_text = _extract_checkstate_target_text(elem)
    name = elem.get("DisplayName", "") or local_name(elem.tag)

    if_exists_elem = None
    if_not_exists_elem = None
    for child in elem:
        child_tag = local_name(child.tag)
        if child_tag.endswith(".IfExists"):
            if_exists_elem = child
        elif child_tag.endswith(".IfNotExists"):
            if_not_exists_elem = child

    branches = [
        {
            "label": "Exists",
            "condition": None,
            "condition_refs": [],
            "steps": _process_steps(_get_real_children(if_exists_elem), ctx) if if_exists_elem is not None else [],
        },
        {
            "label": "Does Not Exist",
            "condition": None,
            "condition_refs": [],
            "steps": _process_steps(_get_real_children(if_not_exists_elem), ctx) if if_not_exists_elem is not None else [],
        },
    ]

    return {
        "type": "branch", "kind": "if", "id": _get_node_id(elem, "NCheckState"),
        "name": name,
        "condition": target_text, "condition_refs": ctx.var_refs(target_text, "read"),
        "branches": branches,
        **({"annotation": _get_annotation(elem)} if _get_annotation(elem) else {}),
    }


def _find_root_activity(root_element: ET.Element) -> ET.Element | None:
    """
    Finds the actual root activity (usually Sequence or Flowchart),
    skipping x:Members and property elements containing dots.
    """
    for child in root_element:
        tag = local_name(child.tag)
        if tag == "Members" or "." in tag:
            continue
        return child
    return None


def extract_activity_flow(root_element: ET.Element, ctx: FlowCtx) -> list[dict]:
    """
    Extracts the FULL activity tree for a file: every activity, structured and nested
    exactly as in the source XML. When the root activity is a Sequence/Flowchart/
    StateMachine, its own children are processed directly without wrapping the root itself
    in a node - but its own <Sequence.Variables>/<StateMachine.Variables> (root-level
    variables) must still be pushed as a scope frame here explicitly, since the generic
    per-node push in _process_activity_node never runs for the root activity in that
    branch. StateMachine.Variables uses a plain <Variable> shape identical to
    Sequence.Variables, so _extract_direct_variable_decls (tag-agnostic - it just looks for
    any *.Variables property) picks it up without modification.

    Flowchart/StateMachine roots delegate to graph_flow.py, since walking their node graph
    (rather than a nested tree) needs a different technique - see the module docstring at
    the top of this file. Imported here, lazily, rather than at module level: graph_flow.py
    imports _process_activity_node/FlowCtx/etc. FROM this module, so importing it back at
    the top of scanner.py would be a circular import; by the time this function actually
    runs, both modules are already fully loaded, so a local import here is safe.
    """
    root_activity = _find_root_activity(root_element)
    if root_activity is None:
        return []

    if local_name(root_activity.tag) in ("Sequence", "Flowchart", "StateMachine"):
        direct_decls = _extract_direct_variable_decls(root_activity)
        raw_frame_info = {
            name: {"decl_id": f"var::root::{name}", "type": info["type"]}
            for name, info in direct_decls.items()
        }
        frame = ctx.register_declarations(raw_frame_info, "Root") if raw_frame_info else {}
        if frame:
            ctx.push(frame)
        try:
            if local_name(root_activity.tag) == "Flowchart":
                from graph_flow import extract_flowchart_steps
                return extract_flowchart_steps(root_activity, ctx)
            if local_name(root_activity.tag) == "StateMachine":
                from graph_flow import extract_statemachine_steps
                return extract_statemachine_steps(root_activity, ctx)
            return _process_steps(_get_real_children(root_activity), ctx)
        finally:
            if frame:
                ctx.pop()

    return [_process_activity_node(root_activity, ctx)]


def parse_xaml_file(
    xaml_path: Path,
    project_root: Path,
    known_paths: set[str],
    by_filename: dict[str, list[str]],
) -> dict:
    """
    Parses a single XAML file and returns its catalog record.
    Logs errors gracefully without halting the scan if parsing fails.

    Imports from invocations.py lazily (see module docstring) rather than at module level,
    since invocations.py imports namespace constants back from this module.
    """
    from invocations import extract_namespaces
    from invocations import resolve_invoke_targets_in_flow, collect_invocations_in_flow_order

    relative_path = xaml_path.relative_to(project_root).as_posix()

    try:
        tree = ET.parse(xaml_path)
        root_element = tree.getroot()
        namespaces = extract_namespaces(xaml_path)
        arguments = extract_arguments(root_element)
        variables = extract_variables(root_element)
        scope_var_names = extract_scope_variable_names(root_element)

        var_names = {a["name"] for a in arguments} | {v["name"] for v in variables} | scope_var_names
        matcher = _build_variable_reference_matcher(var_names)

        declarations: dict = {}
        root_frame: dict = {}
        for a in arguments:
            decl_id = f"arg::{a['name']}"
            root_frame[a["name"]] = decl_id
            declarations[decl_id] = {
                "name": a["name"], "type": a["type"], "scope": "Argument", "direction": a["direction"],
            }

        ctx = FlowCtx(matcher=matcher, scope_stack=[root_frame], declarations=declarations)

        activity_flow = extract_activity_flow(root_element, ctx)
        resolve_invoke_targets_in_flow(activity_flow, xaml_path, project_root, known_paths, by_filename)

        # Derived from activity_flow (after target_relative_path has been resolved on
        # it above) rather than a separate root.iter() pass over the raw XML, so
        # "invokes" inherits activity_flow's already-correct visual/logical order
        # instead of raw document order - see collect_invocations_in_flow_order's
        # docstring in invocations.py for why those two orders can diverge for a
        # graph-shaped root (Flowchart/StateMachine, e.g. a REFramework Main.xaml).
        invocations = collect_invocations_in_flow_order(activity_flow)

        return {
            "relative_path": relative_path,
            "file_name": xaml_path.name,
            "parse_ok": True,
            "root_tag": root_element.tag,
            "namespace_count": len(namespaces),
            "namespaces": namespaces,
            "size_bytes": xaml_path.stat().st_size,
            "arguments": arguments,
            "argument_count": len(arguments),
            "variables": variables,
            "variable_count": len(variables),
            "invokes": invocations,
            "invokes_count": len(invocations),
            "activity_flow": activity_flow,
            "variable_declarations": ctx.declarations,
        }

    except ET.ParseError as e:
        return {
            "relative_path": relative_path,
            "file_name": xaml_path.name,
            "parse_ok": False,
            "error": str(e),
        }


def build_catalog(project_root: Path) -> dict:
    """
    Builds the complete catalog for all XAML files in the project.

    Imports build_dependency_index from invocations.py lazily - see parse_xaml_file and
    the module docstring for why (avoiding a circular import with invocations.py).
    """
    from invocations import build_dependency_index, get_unreachable_paths

    xaml_files = find_xaml_files(project_root)

    # Index every real XAML file in the project up front so invocation targets can be resolved
    # against what's actually there, with a filename-based fallback for bare WorkflowFileName
    # values that don't live next to the invoking file (see resolve_invocation_target).
    relative_paths = [f.relative_to(project_root).as_posix() for f in xaml_files]
    known_paths = set(relative_paths)
    by_filename: dict[str, list[str]] = {}
    for rp in relative_paths:
        by_filename.setdefault(rp.rsplit("/", 1)[-1], []).append(rp)

    entries = [parse_xaml_file(f, project_root, known_paths, by_filename) for f in xaml_files]
    build_dependency_index(entries)

    # Drop files that aren't reachable from Main.xaml (directly or transitively via
    # InvokeWorkflowFile) - these are leftover/unused workflows that don't belong to the
    # project's real structure and shouldn't appear on the map as if they were key files.
    # Computed against the full entry set before any filtering happens, so this must run
    # right after build_dependency_index and before the failed/unresolved summaries below,
    # which should describe only what's actually kept.
    unreachable = get_unreachable_paths(entries)
    entries = [e for e in entries if e["relative_path"] not in unreachable]

    failed = [e for e in entries if not e["parse_ok"]]

    unresolved_invocations = []
    for entry in entries:
        if not entry.get("parse_ok"):
            continue
        for inv in entry.get("invokes", []):
            if inv["target_relative_path"] is None:
                unresolved_invocations.append(
                    {
                        "from_file": entry["relative_path"],
                        "raw_path": inv["raw_path"],
                        "is_dynamic": inv["is_dynamic"],
                    }
                )

    return {
        "project_root": str(project_root),
        "total_xaml_files": len(entries),
        "parsed_successfully": len(entries) - len(failed),
        "failed_to_parse": len(failed),
        "unreachable_from_main_excluded": sorted(unreachable),
        "unresolved_invocations": unresolved_invocations,
        "files": entries,
    }