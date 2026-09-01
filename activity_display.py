"""
UiPath XAML Scanner - Per-Activity Display Customization

Single home for every rule about how a specific activity TAG should be reshaped and
displayed - as opposed to scanner.py, which holds the generic, tag-agnostic extraction
machinery (walking children, tracking variable scope, resolving expressions to
declarations) that every activity goes through regardless of its tag.

THIS is the file to touch when a newly-encountered activity needs custom handling:
  - Add (or edit) one entry in ACTIVITY_SPECS below, keyed by the activity's tag.
  - `fields`: which arguments/attributes should surface as key_arguments, and how (see
    FieldSpec) - default view, Full View, or only when the field actually has a value.
  - `loop`: only for activities that repeat a body under a condition (While, DoWhile,
    InterruptibleDoWhile, and any future loop-shaped activity). Set this when the
    activity's Condition should render in a branch header like If, rather than as an
    ordinary key_arguments row - see LoopSpec for the two shapes a condition can take in
    XAML (a plain attribute, or a property element wrapping VisualBasicValue/
    ExpressionText).
Nothing outside this file needs to change for either kind of addition -
scanner.py's _process_activity_node dispatches through ACTIVITY_SPECS generically (see
its own docstring), and drawer_fragment.html reads the "compact"/"full_view_only" flags
this file stamps onto each field, with no per-tag branches of its own.

Public interface (imported by scanner.py):
  - FieldSpec, LoopSpec, ActivitySpec - the three building blocks of one registration.
  - ACTIVITY_SPECS - the registry itself, keyed by tag.
  - select_key_arguments(all_args, tag) -> list[dict] | None - applies a tag's `fields`
    to a raw-arguments list, stamping "compact"/"full_view_only" on each kept field.
  - humanize_keyboard_shortcuts(raw) -> str - NKeyboardShortcuts' own macro-syntax
    formatter, kept here (rather than in scanner.py) since it only ever runs on a field
    ACTIVITY_SPECS itself curates (NKeyboardShortcuts' "Shortcuts") - scanner.py calls it
    once, right after select_key_arguments, to reformat that one field's value in place.

Dependency direction: this module has NO dependency on scanner.py - it operates purely on
plain dicts (raw argument lists) and tag strings, never on ET.Element or FlowCtx. That
keeps it usable (and testable) independent of XML parsing, and avoids adding a new edge to
scanner.py's existing circular-import dance with graph_flow.py/invocations.py.
"""

import re
from typing import NamedTuple


class FieldSpec(NamedTuple):
    """
    Describes how a single argument/attribute of a given activity tag should surface in
    the UI. Replaces the old flat KEY_ARGS_BY_TAG name-list, which had no way to express
    "only in Full View" or "only when it has a value" - those used to require bespoke
    code in scanner.py (a curated tag set) AND in drawer_fragment.html
    (FULL_VIEW_ONLY_KEY_ARG_TAGS) to express even one such rule. Now the Python side
    computes the full display decision once, per field, and stamps it onto the field's
    own dict (see select_key_arguments) - the frontend just reads it back, tag-agnostic.

    name: the argument/attribute name as it appears in scanner.py's
        _extract_activity_raw_arguments output (i.e. after the "Tag." prefix has been
        stripped, or the bare attribute name for plain XML attributes).
    compact: shown in the default (compact) view.
    full: shown in Full View. (A field with compact=False, full=True is Full-View-only -
        this is what Throw's Exception used to need FULL_VIEW_ONLY_KEY_ARG_TAGS for.)
    only_if_present: if True, the field is omitted entirely (in BOTH views) when it has
        no value, rather than showing with a blank/empty value.
    """
    name: str
    compact: bool = True
    full: bool = True
    only_if_present: bool = False


class LoopSpec(NamedTuple):
    """
    Describes how to pull a "while"-shaped branch node's condition text out of a loop
    activity's XAML, for tags registered as loops in ACTIVITY_SPECS (see its docstring).
    A loop's condition can show up in either of two unrelated shapes across UiPath's own
    activity set, and a single tag/registration point needs to say which:

    condition_attr: the condition lives in a plain XML attribute on the activity itself
        (e.g. <While Condition="...">). Set this OR condition_property, never both.
    condition_property: the condition lives in a property element (e.g.
        <ui:InterruptibleDoWhile.Condition><VisualBasicValue ExpressionText="..."/>
        </ui:InterruptibleDoWhile.Condition>) - pass the property's local suffix after
        the tag prefix, i.e. "Condition" for "InterruptibleDoWhile.Condition". Resolved
        by scanner.py's _process_while_node via _extract_condition_expression, which
        handles both the simple InArgument-wrapper-with-text-content shape and the
        VisualBasicValue/VisualBasicReference-wrapper-with-ExpressionText-attribute
        shape, since different UiPath activities emit either one for what is
        conceptually the same condition. Confirmed against real projects that
        InterruptibleDoWhile itself can ALSO write Condition as a plain attribute
        instead (same attribute name given here) rather than this property-element
        form - apparently both occur for this one tag, not just across different tags -
        so _process_while_node falls back to reading it as a plain attribute (using
        this same name) whenever the property element isn't present. That fallback
        means condition_property alone is enough for a tag that can appear in either
        shape; a fresh tag that's ONLY ever the plain-attribute shape should still use
        condition_attr instead, since that documents the actual shape rather than
        relying on a fallback path meant for the mixed case.
    """
    condition_attr: str | None = None
    condition_property: str | None = None


class ActivitySpec(NamedTuple):
    """
    Single per-tag registration point: everything needed to both reshape an activity's
    XAML into a flow-tree node AND decide what of it surfaces in the compact vs. Full
    View - see the module-level ACTIVITY_SPECS registry below for the actual entries.
    Adding a newly-encountered activity tag (or a new field/condition shape on one
    already registered) should only ever require one new/edited entry here - not a
    change to scanner.py's _process_activity_node dispatch, not a change to
    drawer_fragment.html.

    loop: set for activities that repeat a body under a condition (While, DoWhile,
        InterruptibleDoWhile, and any future loop-shaped activity) - see LoopSpec. When
        set, scanner.py's _process_activity_node routes the tag through
        _process_while_node, which builds a "branch"/kind:"while" node with the
        condition in the header (like If) and this tag's `fields` as key_arguments on
        the branch itself (loop parameters like MaxIterations/Index, not the condition).
        Leave unset for anything that isn't loop-shaped - it stays on the plain
        activity/container path, and `fields` is applied there instead (as today's
        key_arguments on the activity node itself).
    fields: the FieldSpec list for this tag - loop parameters for a loop, or ordinary
        key arguments for anything else. Defaults to an empty list (no curated fields);
        use ACTIVITY_SPECS.get(tag) is None (not an empty `fields` list) to tell "no
        entry at all for this tag" from "registered, but nothing to curate" - see
        select_key_arguments and scanner.py's _process_activity_node.
    """
    fields: list[FieldSpec] = []
    loop: LoopSpec | None = None


# --- Single per-tag customization point -------------------------------------------------
# Every activity tag needing special display handling - a curated set of fields, a
# non-obvious condition shape, or both - is registered here ONCE, as one ActivitySpec.
# This is the dict (and the only dict) to touch when a new activity needs custom
# handling: add a tag, its FieldSpec list, and (only for loop-shaped activities) a
# LoopSpec describing where its condition actually lives in XAML. Nothing elsewhere
# needs to change - scanner.py's dispatch and drawer_fragment.html's rendering both read
# this registry generically, tag-agnostic.
ACTIVITY_SPECS: dict[str, ActivitySpec] = {
    "TypeInto": ActivitySpec(fields=[FieldSpec("Text")]),
    "SendHotkey": ActivitySpec(fields=[FieldSpec("Key"), FieldSpec("KeyModifiers", only_if_present=True)]),
    "NKeyboardShortcuts": ActivitySpec(fields=[FieldSpec("Shortcuts")]),
    "Click": ActivitySpec(fields=[FieldSpec("Selector")]),
    "GetText": ActivitySpec(fields=[FieldSpec("Value")]),
    "SetText": ActivitySpec(fields=[FieldSpec("Text")]),
    "WriteLine": ActivitySpec(fields=[FieldSpec("Text")]),
    "LogMessage": ActivitySpec(fields=[FieldSpec("Message")]),
    "AppendLine": ActivitySpec(fields=[FieldSpec("Text")]),
    "ForEach": ActivitySpec(fields=[FieldSpec("Values")]),
    "WaitElementVisible": ActivitySpec(fields=[FieldSpec("Selector")]),
    "ElementExists": ActivitySpec(fields=[FieldSpec("Selector")]),
    "Delay": ActivitySpec(fields=[FieldSpec("Duration")]),
    "MessageBox": ActivitySpec(fields=[FieldSpec("Text")]),
    "InputDialog": ActivitySpec(fields=[FieldSpec("Title")]),
    "RetryScope": ActivitySpec(fields=[FieldSpec("NumberOfRetries")]),
    "KillProcess": ActivitySpec(fields=[FieldSpec("ProcessName")]),
    "ExcelProcessScope": ActivitySpec(fields=[]),
    "ExcelApplicationScope": ActivitySpec(fields=[]),
    # DataTable/OutputDataTable were already showing via the reference-only fallback
    # (since they usually reference a known DataTable variable) - registered here now
    # too so they're no longer at the mercy of that heuristic, and so the actual filter
    # conditions (built separately - see build_filter_data_table_key_arguments) render
    # alongside them as ordinary key_arguments rather than a mix of two mechanisms.
    "FilterDataTable": ActivitySpec(fields=[FieldSpec("DataTable"), FieldSpec("OutputDataTable")]),
    # --- Modern Design Experience / descriptor-based activities (the "N"-prefixed uix:
    # namespace UiPath switched to - NTypeInto, NClick, NGetText, etc.) -----------------
    # These are DIFFERENT TAGS from their classic counterparts above (NTypeInto vs.
    # TypeInto), not a renamed/aliased version of the same one - a project built with
    # the Modern experience uses these tags exclusively, so without their own
    # registration here they fall through entirely unregistered. That's a real gap, not
    # a hypothetical one: ACTIVITY_SPECS.get(tag) is None routes to the
    # reference-only fallback (_filter_variable_referencing in scanner.py), which only
    # keeps a field when its value happens to mention a known variable - so a static,
    # developer-typed value (e.g. Text="parameter", not "[myVar]") was silently dropped
    # in BOTH views, not just the compact one, since with no ACTIVITY_SPECS entry
    # there's no key_arguments at all for the field to land in.
    #
    # IMPORTANT: an "N" tag does NOT always reuse its classic counterpart's own
    # attribute NAME, even when the concept is identical - NGetText's output is
    # TextString, not Value (GetText's own name for the same thing), confirmed against
    # a real project's XAML. Verify each attribute name against an actual sample of the
    # tag rather than assuming it matches the classic activity - that's exactly the
    # mistake NGetText's own entry originally made here (registered as "Value", which
    # doesn't exist on NGetText at all, so it silently showed nothing).
    "NTypeInto": ActivitySpec(fields=[FieldSpec("Text")]),
    "NClick": ActivitySpec(fields=[FieldSpec("Selector")]),
    "NGetText": ActivitySpec(fields=[FieldSpec("TextString")]),
    "NSetText": ActivitySpec(fields=[FieldSpec("Text")]),
    "NElementExists": ActivitySpec(fields=[FieldSpec("Selector")]),
    "NWaitElementVisible": ActivitySpec(fields=[FieldSpec("Selector")]),
    # Throw's error message is the whole point of the node - it's a literal string
    # (e.g. New Exception("message")), not a variable reference, so without a field spec
    # scanner.py's _filter_variable_referencing silently drops it (it only keeps
    # arguments that reference a known variable), leaving the node with no visible
    # content at all. Shown in the default (compact) view too - not Full-View-only -
    # since the Throw node itself always renders regardless of view (it's structural
    # control flow, like If/Switch), so hiding the one thing that explains WHY it
    # throws wastes exactly the screen space the node was already given.
    "Throw": ActivitySpec(fields=[FieldSpec("Exception", compact=True, full=True)]),
    "Rethrow": ActivitySpec(fields=[]),
    # --- Loop-shaped activities ---------------------------------------------------------
    # Every entry below has `loop` set, which routes it through scanner.py's
    # _process_while_node instead of the generic activity/container path - see
    # ActivitySpec's docstring. `fields` here means loop parameters (shown as
    # key_arguments on the branch node), never the condition itself (that's LoopSpec's
    # job, surfaced in the branch header).
    #
    # Studio's plain <While>/<DoWhile> activities put Condition/Index directly on the
    # element as XML attributes.
    "While": ActivitySpec(
        loop=LoopSpec(condition_attr="Condition"),
        fields=[
            FieldSpec("MaxIterations", compact=False, full=True, only_if_present=True),
            FieldSpec("Index", compact=True, full=True, only_if_present=True),
        ],
    ),
    "DoWhile": ActivitySpec(
        loop=LoopSpec(condition_attr="Condition"),
        fields=[
            FieldSpec("MaxIterations", compact=False, full=True, only_if_present=True),
            FieldSpec("Index", compact=True, full=True, only_if_present=True),
        ],
    ),
    # UiPath Studio's actual "Do While" activity in the modern activity package is
    # ui:InterruptibleDoWhile, not the plain WF DoWhile above - both are registered since
    # older/converted projects can still contain the plain form. Its shape differs on
    # both axes ActivitySpec exists to cover: Condition is a property element
    # (InterruptibleDoWhile.Condition) wrapping a VisualBasicValue/ExpressionText rather
    # than a plain attribute (see LoopSpec.condition_property), and its loop-index
    # attribute is spelled CurrentIndex rather than Index. Body is still a plain
    # ".Body"-suffixed property element, so scanner.py's _get_real_children (via its
    # existing _CONTAINER_BODY_SUFFIXES handling) already unwraps it with no extra code
    # needed.
    "InterruptibleDoWhile": ActivitySpec(
        loop=LoopSpec(condition_property="Condition"),
        fields=[
            FieldSpec("MaxIterations", compact=False, full=True, only_if_present=True),
            FieldSpec("CurrentIndex", compact=True, full=True, only_if_present=True),
        ],
    ),
}


def select_key_arguments(all_args: list[dict], tag: str) -> list[dict] | None:
    """
    Returns the curated subset of arguments for tags with a known ACTIVITY_SPECS entry,
    each stamped with "compact" (and, where relevant, dropped entirely per
    only_if_present) per its FieldSpec - so the frontend never needs a single
    tag-specific branch to decide what's visible in which view (see FieldSpec's
    docstring). Returns None for tags without a registration, so scanner.py can fall
    back to showing everything in Full View rather than showing nothing.

    NOTE for any caller building a key_arguments list BY HAND instead of through this
    function (e.g. scanner.py's InvokeCode handling, whose two content sources - a plain
    Code attribute and a multi-entry <ui:InvokeCode.Arguments> property element - don't
    fit a single FieldSpec): every hand-built entry MUST still set "compact" itself
    (True for anything meant to show in the default view), since drawer_fragment.html's
    renderArgRows only shows a key_arguments row in the default view when "compact" is
    truthy. Forgetting this is an easy, silent regression - a hand-built field with no
    "compact" key reads as falsy and the row simply vanishes from the compact view,
    which is exactly what happened to InvokeCode's Code/argument rows the first time
    this registry replaced the old KEY_ARGS_BY_TAG mechanism (that mechanism's
    equivalent hand-built entries had the same requirement, just less explicitly).
    """
    specs = ACTIVITY_SPECS.get(tag)
    if specs is None:
        return None
    fields = specs.fields
    by_name = {a["name"]: a for a in all_args}
    selected: list[dict] = []
    for spec in fields:
        arg = by_name.get(spec.name)
        has_value = arg is not None and str(arg.get("value", "")).strip() != ""
        if spec.only_if_present and not has_value:
            continue
        if arg is None:
            continue
        arg = dict(arg)
        arg["compact"] = spec.compact
        arg["full_view_only"] = spec.full and not spec.compact
        selected.append(arg)
    return selected


# Matches one bracketed macro token - [d(key)] (key down), [u(key)] (key up), or
# [k(key)] (momentary tap) - or, as the unmatched fallback branch, a run of literal
# characters sitting outside brackets (NKeyboardShortcuts allows typing plain text
# between key-down/key-up pairs, e.g. the "c" in "[d(ctrl)]c[u(ctrl)]" for Ctrl+C).
_SHORTCUT_TOKEN_RE = re.compile(r"\[(d|u|k)\(([^)]*)\)\]|([^\[\]]+)")

# NKeyboardShortcuts' own fixed activation modifier - present in [d(...)]/[u(...)] on
# every single shortcut this activity fires (it's how the app-under-automation enters
# "shortcut mode" at all), so unlike a real modifier such as Ctrl or Alt it never
# distinguishes one shortcut from another. Spelling it out on every combo would just be
# noise; only the key(s) the developer actually paired it with are worth showing.
_SHORTCUT_OMIT_KEYS = {"hk"}


def humanize_keyboard_shortcuts(raw: str) -> str:
    """
    Converts NKeyboardShortcuts' raw macro syntax (e.g.
    "[d(hk)][d(ctrl)]c[u(ctrl)][u(hk)]") into the plain key-combo text a person actually
    reads off the activity in Studio (e.g. "Ctrl + C") - the bracket/d/u/k notation is an
    internal press/release encoding, not something meant for a developer's eyes, and the
    "hk" modifier it always wraps every shortcut in (see _SHORTCUT_OMIT_KEYS) is dropped
    entirely since it never varies.

    Keys held down via [d(key)] accumulate into the combo being built; [k(key)] taps a
    key without holding it; plain text outside brackets is typed literally. A combo is
    considered complete once every currently-held key has been released via [u(key)] (or
    immediately, for a [k(...)]/literal that closes with nothing held) - at which point
    it's flushed into the output list. Consecutive identical combos (the same shortcut
    fired back-to-back, as UiPath sometimes encodes a single repeated keystroke) are
    collapsed into one entry with a "×N" suffix rather than repeating the same text N times.

    Falls back to returning `raw` unchanged if nothing recognizable was found, so a syntax
    variant this parser doesn't know about still shows something rather than going blank.
    """
    if not raw:
        return raw

    combos: list[str] = []
    held: list[str] = []
    current: list[str] | None = None

    def visible(keys: list[str]) -> list[str]:
        return [k for k in keys if k.lower() not in _SHORTCUT_OMIT_KEYS]

    def flush() -> None:
        nonlocal current
        if current:
            combos.append(" + ".join(current))
        current = None

    for action, key, literal in _SHORTCUT_TOKEN_RE.findall(raw):
        if literal:
            literal = literal.strip()
            if not literal:
                continue
            if current is None:
                current = visible(held)
            current.append(literal.upper() if len(literal) == 1 else literal.title())
            flush()
            continue

        key_label = key.title()
        if action == "d":
            held.append(key_label)
            if current is None:
                current = visible(held)
            elif key_label.lower() not in _SHORTCUT_OMIT_KEYS:
                current = current + [key_label]
        elif action == "k":
            if current is None:
                current = visible(held)
            if key_label.lower() not in _SHORTCUT_OMIT_KEYS:
                current = current + [key_label]
            flush()
        elif action == "u":
            if key_label in held:
                held.remove(key_label)
            if not held:
                flush()

    flush()

    if not combos:
        return raw

    display: list[str] = []
    i = 0
    while i < len(combos):
        j = i
        while j < len(combos) and combos[j] == combos[i]:
            j += 1
        count = j - i
        display.append(combos[i] if count == 1 else f"{combos[i]} \u00d7{count}")
        i = j

    return ", ".join(display)


# XAML's literal marker for "this argument exists but has no value set". Duplicated from
# scanner.py's own _NULL_MARKERS (rather than imported) to keep this module's declared
# zero-dependency-on-scanner.py promise (see the module docstring) - it's a one-line XML
# convention, not scanner-specific logic, so a second copy costs far less than a new
# import edge back into scanner.py would.
_NULL_MARKERS = {"{x:Null}", ""}


def build_invoke_code_key_arguments(code_text: str, bindings: list[dict]) -> list[dict]:
    """
    Builds InvokeCode's key_arguments rows from its already-extracted raw pieces (the
    Code attribute's text, and its parameter bindings from <ui:InvokeCode.Arguments>).
    Pure shape-building only - takes plain strings/dicts, returns plain dicts - callers
    (scanner.py) still own extracting code_text/bindings from the XML and computing each
    row's "value_refs" (both need FlowCtx/ET.Element, which this module deliberately has
    no dependency on - see the module docstring).

    InvokeCode isn't in ACTIVITY_SPECS because its two pieces of content don't fit the
    FieldSpec mechanism: "Code" is a plain attribute whose generic variable-reference
    filter would only keep it if the code text happens to namedrop a known variable by
    name - the code itself is what matters here, not whether it does - and its parameter
    bindings live in a multi-entry property element that a single FieldSpec can't
    unpack. This function exists so that even though InvokeCode's key_arguments can't be
    built by select_key_arguments, the actual ROW SHAPE (name/value/direction/compact)
    is still defined here, in the one file responsible for what a key_arguments row
    looks like - not scattered back into scanner.py's dispatch.

    Every entry here is stamped "compact": True by hand, matching what
    select_key_arguments would stamp for an ordinary FieldSpec(name) entry (compact
    defaults to True there too) - drawer_fragment.html's renderArgRows only shows a
    key_arguments row in the default view when "compact" is truthy, so any hand-built
    key_arguments list (this one, or any future one) MUST set it explicitly or the row
    silently vanishes from the compact view. (This is exactly the regression that
    prompted pulling this function out on its own: the first version of InvokeCode's
    key_arguments handling, written directly in scanner.py before ACTIVITY_SPECS
    existed, forgot this and its rows vanished from the compact view.)

    code_text: the raw text of InvokeCode's Code attribute, already extracted by the
        caller (elem.get("Code", "")) - empty/null-marker values should still be passed
        through; this function does its own emptiness check.
    bindings: the list of dicts from invocations.extract_invoke_code_arguments, each
        already carrying "name"/"direction"/"expression" - but NOT yet "value_refs",
        which the caller must add to each returned row afterward (see scanner.py's call
        site) since it needs FlowCtx to compute.

    Returns a list of rows with name/value/direction/compact set - the caller adds
    "value_refs" to each row in place before attaching the list as node["key_arguments"].
    """
    rows: list[dict] = []
    if code_text.strip() and code_text not in _NULL_MARKERS:
        rows.append({"name": "Code", "value": code_text, "direction": None, "compact": True})
    for binding in bindings:
        rows.append({
            "name": binding["name"],
            "value": binding["expression"],
            "direction": binding["direction"],
            "compact": True,
        })
    return rows


def merge_send_hotkey_key_arguments(key_args: list[dict]) -> list[dict]:
    """
    Post-processes SendHotkey's already-curated key_arguments (Key + KeyModifiers, both
    registered in ACTIVITY_SPECS under "SendHotkey") into a single combined row, the way
    a person actually reads a hotkey off the activity in Studio - "Ctrl + Left", not two
    separate "Key: left" / "KeyModifiers: Ctrl" rows a reader has to mentally recombine.
    Called from scanner.py right after select_key_arguments, the identical pattern
    humanize_keyboard_shortcuts already uses for NKeyboardShortcuts' own combo field -
    see that function's docstring for the analogous reasoning.

    KeyModifiers can hold more than one modifier for chords like Ctrl+Shift+Left -
    UiPath separates them with a comma (e.g. "Ctrl, Shift") - so each is split, title-
    cased, and joined with " + " ahead of the key itself, mirroring the "Modifier + ...
    + Key" ordering Studio's own hotkey picker displays.

    UiPath writes the literal string "None" for KeyModifiers when no modifier checkbox
    is checked - not the {x:Null} marker _NULL_MARKERS elsewhere in this file exists to
    catch, and not an empty string either - so it survives extraction as a real value
    and, left unhandled, would combine into a nonsensical "None + Home" row. Treated the
    same as "no modifiers at all" here for that reason.

    Takes/returns the same list-of-dicts shape select_key_arguments produces (so
    scanner.py can call this in place, the same way it already special-cases
    NKeyboardShortcuts) - if Key is missing (shouldn't happen; SendHotkey's Key can't be
    empty in Studio) this simply returns key_args unchanged rather than guessing.
    """
    key_row = next((a for a in key_args if a["name"] == "Key"), None)
    if key_row is None:
        return key_args
    modifiers_row = next((a for a in key_args if a["name"] == "KeyModifiers"), None)
    has_modifiers = (
        modifiers_row
        and modifiers_row.get("value")
        and modifiers_row["value"].strip().lower() != "none"
    )

    if has_modifiers:
        parts = [p.strip().title() for p in modifiers_row["value"].split(",") if p.strip()]
        key_label = key_row["value"].title() if len(key_row["value"]) > 1 else key_row["value"].upper()
        combined_value = " + ".join(parts + [key_label])
    else:
        combined_value = key_row["value"]

    merged_row = dict(key_row)
    merged_row["value"] = combined_value
    return [merged_row] + [a for a in key_args if a["name"] not in ("Key", "KeyModifiers")]


def build_filter_data_table_key_arguments(conditions: list[dict]) -> list[dict]:
    """
    Builds FilterDataTable's key_arguments rows for its filter conditions (see
    scanner.py's extract_filter_data_table_conditions for how those are pulled out of
    <ui:FilterDataTable.Filters>). Not in ACTIVITY_SPECS as an ordinary FieldSpec
    because Filters is a multi-entry property element (zero or more conditions) rather
    than a single value - the same reason InvokeCode's Arguments needed its own
    build_invoke_code_key_arguments instead of a FieldSpec.

    One row per condition (matching how DataTable/OutputDataTable already render as
    separate rows), each combining column/operator/operand into a single readable
    string - e.g. "asdf CONTAINS 2233" - rather than three separate rows a reader would
    have to mentally recombine, the same "one combined row over several raw fields"
    call already made for SendHotkey's Key+KeyModifiers (see
    merge_send_hotkey_key_arguments) and NKeyboardShortcuts' Shortcuts (see
    humanize_keyboard_shortcuts). A condition's own boolean_operator ("And"/"Or" - how
    it combines with the PREVIOUS condition) is prefixed onto every condition except the
    first, e.g. a second row might read "And col2 = val2" - the first condition never
    gets a prefix since there's nothing before it to combine with.

    All rows are stamped "compact": True (shown in the default view, not just Full
    View) - per the request that prompted this function, FilterDataTable's actual
    filtering logic (which rows get kept/removed) is exactly the kind of "structural
    logic understanding" the default view exists to surface, the same reasoning that
    already governs If/Switch/While conditions and Throw's Exception message.

    conditions: the list from scanner.py's extract_filter_data_table_conditions, each
        already carrying column/operator/operand/boolean_operator - but not yet a
        "value_refs" per field, which the caller must add afterward (see scanner.py's
        call site) since it needs FlowCtx to compute; each returned row also carries a
        "column"/"operand" pair under "_refs_source" so the caller knows which two raw
        expressions to resolve into that row's value_refs.
    """
    rows: list[dict] = []
    for i, cond in enumerate(conditions):
        prefix = f"{cond['boolean_operator']} " if i > 0 and cond.get("boolean_operator") else ""
        text = f"{prefix}{cond['column']} {cond['operator']} {cond['operand']}".strip()
        rows.append({
            "name": "Filter" if len(conditions) == 1 else f"Filter {i + 1}",
            "value": text,
            "direction": None,
            "compact": True,
            "_refs_source": (cond["column"], cond["operand"]),
        })
    return rows