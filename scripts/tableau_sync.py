#!/usr/bin/env python3
"""tableau_sync.py - Synchronise Tableau .twbx files between main (Live/) and customer (Live/Customers/) versions.

Usage:
    python scripts/tableau_sync.py generate-diff              # Diff main vs customer -> write contract.json
    python scripts/tableau_sync.py apply-diff                 # Rebuild customer = copy(main) + contract
    python scripts/tableau_sync.py apply-diff --file X.twbx   # Process one specific file
    python scripts/tableau_sync.py repair                     # Remove duplicate layout elements from customer files
    python scripts/tableau_sync.py repair --file X.twbx       # Repair one specific customer file
    python scripts/tableau_sync.py pre-commit                 # Called automatically by git pre-commit hook
"""

import argparse
import io
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_DIR = REPO_ROOT / "Live"
CUSTOMERS_DIR = LIVE_DIR / "Customers"
DIFFERENCES_FILE = "contract.json"

# Attributes checked (in priority order) to build a stable identity key for an element.
# Elements with these attributes can be matched by value across file versions.
_KEY_ATTRS = ("name", "column", "attr", "id", "param", "caption")

# Tableau internal layout tags whose IDs are regenerated on every save.
# Structural add/remove of these elements must never be propagated — doing so
# duplicates elements that already exist in the customer file.
_TABLEAU_LAYOUT_TAGS = frozenset({"zone", "pane", "window", "point", "size"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_twbx_pairs() -> List[Tuple[Path, Path]]:
    """Return (main_path, customer_path) for every matching .twbx pair."""
    pairs = []
    for main_file in sorted(LIVE_DIR.glob("*.twbx")):
        customer_file = CUSTOMERS_DIR / main_file.name
        if customer_file.exists():
            pairs.append((main_file, customer_file))
    return pairs


def extract_twb(twbx_path: Path, dest_dir: Path) -> Path:
    """Extract the .twb XML file from a .twbx ZIP archive into dest_dir.
    Returns the absolute path to the extracted .twb file."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(twbx_path, "r") as z:
        z.extractall(dest_dir)
    twb_files = list(dest_dir.rglob("*.twb"))
    if not twb_files:
        raise FileNotFoundError("No .twb file found inside " + str(twbx_path))
    return twb_files[0]


def extract_twb_from_bytes(data: bytes, dest_dir: Path) -> Path:
    """Extract the .twb XML file from raw twbx bytes (e.g. from git show) into dest_dir.
    Returns the absolute path to the extracted .twb file."""
    import io
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data), "r") as z:
        z.extractall(dest_dir)
    twb_files = list(dest_dir.rglob("*.twb"))
    if not twb_files:
        raise FileNotFoundError("No .twb file found in git-retrieved twbx bytes")
    return twb_files[0]


def repack_twbx(source_dir: Path, output_path: Path) -> None:
    """Repack all files in source_dir into a .twbx ZIP at output_path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as z:
        for file_path in sorted(source_dir.rglob("*")):
            if file_path.is_file():
                z.write(file_path, file_path.relative_to(source_dir))


# ---------------------------------------------------------------------------
# generate-diff
# ---------------------------------------------------------------------------

def generate_diff() -> None:
    """Compare each main .twbx with its customer counterpart and write contract.json."""
    pairs = find_twbx_pairs()
    if not pairs:
        print("No matching .twbx pairs found in Live/ and Live/Customers/.", file=sys.stderr)
        sys.exit(1)

    all_diffs: Dict = {}

    for main_path, customer_path in pairs:
        print(
            "Comparing: "
            + str(main_path.relative_to(REPO_ROOT))
            + "  vs  "
            + str(customer_path.relative_to(REPO_ROOT))
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            main_dir = tmp_dir / "main"
            cust_dir = tmp_dir / "customer"

            main_twb = extract_twb(main_path, main_dir)
            cust_twb = extract_twb(customer_path, cust_dir)
            twb_filename = main_twb.name

            main_tree = ET.parse(main_twb)
            cust_tree = ET.parse(cust_twb)

            modifications = _diff_trees(main_tree, cust_tree)

            # Strip ephemeral Tableau layout elements (zone/pane/window IDs are
            # regenerated on every save and must never be stored in the contract).
            modifications = [m for m in modifications if not _is_layout_structural(m)]

            attr_changes = sum(1 for m in modifications if m["action"] == "change_attribute")
            adds = sum(1 for m in modifications if m["action"] == "add_element")
            removes = sum(1 for m in modifications if m["action"] == "remove_element")
            print(
                "  -> "
                + str(attr_changes) + " attribute change(s), "
                + str(adds) + " element addition(s), "
                + str(removes) + " element removal(s)."
            )

            all_diffs[main_path.name] = {
                "description": (
                    "Differences between main "
                    + main_path.name
                    + " and customer "
                    + customer_path.name
                ),
                "xml_changes": {
                    twb_filename: {
                        "xpath_modifications": modifications,
                    }
                },
            }

    out_path = CUSTOMERS_DIR / DIFFERENCES_FILE
    out_path.write_text(json.dumps(all_diffs, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Saved " + str(out_path.relative_to(REPO_ROOT)))


# ---------------------------------------------------------------------------
# XML diffing helpers
# ---------------------------------------------------------------------------

def _get_key(elem: ET.Element) -> Optional[Tuple[str, str]]:
    """Return (attr_name, attr_value) that identifies this element, or None."""
    for attr in _KEY_ATTRS:
        if attr in elem.attrib:
            return (attr, elem.attrib[attr])
    return None


def _make_selector(elem: ET.Element) -> str:
    """Return an XPath selector string for this element: tag[@key="val"] or just tag."""
    key = _get_key(elem)
    if key:
        attr_name, attr_val = key
        # Use single quotes inside to avoid escaping issues
        safe_val = attr_val.replace("'", "\\'")
        return elem.tag + "[@" + attr_name + "='" + safe_val + "']"
    return elem.tag


def _diff_trees(main_tree: ET.ElementTree, cust_tree: ET.ElementTree) -> List[Dict]:
    """Recursively diff two XML trees.

    Returns a list of modifications, each being one of:
      - change_attribute: an element attribute differs between main and customer
      - add_element:      an element exists in customer but not in main
      - remove_element:   an element exists in main but not in customer
    """
    modifications: List[Dict] = []
    seen_attr_changes: Set[FrozenSet] = set()
    main_root = main_tree.getroot()
    cust_root = cust_tree.getroot()
    _diff_children(main_root, cust_root, ".", modifications, seen_attr_changes)
    return modifications


def _diff_attrs(
    main_elem: ET.Element,
    cust_elem: ET.Element,
    modifications: List[Dict],
    seen: Set[FrozenSet],
    element_xpath: str = "",
) -> None:
    """Detect attribute-level differences between two aligned elements and append modifications.

    element_xpath: full XPath to this element from the root (used for precise targeting).
    When provided, the modification targets only this exact element instead of all matching
    elements across the document.
    """
    key = _get_key(main_elem)
    attr_filter = {key[0]: key[1]} if key else {}

    for attr, main_val in main_elem.attrib.items():
        cust_val = cust_elem.attrib.get(attr)
        if cust_val is not None and cust_val != main_val:
            dedup_key = frozenset([
                ("tag", main_elem.tag),
                ("attribute", attr),
                ("old_value", main_val),
                ("new_value", cust_val),
                ("filter", str(sorted(attr_filter.items()))),
                ("xpath", element_xpath),
            ])
            if dedup_key not in seen:
                seen.add(dedup_key)
                mod: Dict = {
                    "action": "change_attribute",
                    "tag": main_elem.tag,
                    "attribute": attr,
                    "old_value": main_val,
                    "new_value": cust_val,
                    "attr_filter": attr_filter,
                }
                if element_xpath:
                    mod["element_xpath"] = element_xpath
                modifications.append(mod)


def _diff_children(
    main_elem: ET.Element,
    cust_elem: ET.Element,
    parent_xpath: str,
    modifications: List[Dict],
    seen: Set[FrozenSet],
) -> None:
    """Align children of two matched elements, recurse, and record structural differences."""
    # Group children by tag
    def _group(elem: ET.Element) -> Dict:
        by_tag: Dict = {}
        for child in elem:
            by_tag.setdefault(child.tag, []).append(child)
        return by_tag

    main_by_tag = _group(main_elem)
    cust_by_tag = _group(cust_elem)
    all_tags = set(main_by_tag) | set(cust_by_tag)

    for tag in sorted(all_tags):
        main_list = main_by_tag.get(tag, [])
        cust_list = cust_by_tag.get(tag, [])

        # Partition into keyed (matched by identity attr) and positional (matched by index)
        main_keyed: Dict[Tuple[str, str], ET.Element] = {}
        main_positional: List[ET.Element] = []
        for elem in main_list:
            k = _get_key(elem)
            if k:
                main_keyed[k] = elem
            else:
                main_positional.append(elem)

        cust_keyed: Dict[Tuple[str, str], ET.Element] = {}
        cust_positional: List[ET.Element] = []
        for elem in cust_list:
            k = _get_key(elem)
            if k:
                cust_keyed[k] = elem
            else:
                cust_positional.append(elem)

        # --- Keyed elements ---
        all_keys = set(main_keyed) | set(cust_keyed)
        for k in sorted(all_keys):
            main_child = main_keyed.get(k)
            cust_child = cust_keyed.get(k)
            attr_name, attr_val = k
            safe_val = attr_val.replace("'", "\\'")
            child_xpath = parent_xpath + "/" + tag + "[@" + attr_name + "='" + safe_val + "']"

            if main_child is None and cust_child is not None:
                # Element added in customer version
                modifications.append({
                    "action": "add_element",
                    "parent_xpath": parent_xpath,
                    "xml": ET.tostring(cust_child, encoding="unicode"),
                })
            elif main_child is not None and cust_child is None:
                # Element removed in customer version
                modifications.append({
                    "action": "remove_element",
                    "parent_xpath": parent_xpath,
                    "element_tag": tag,
                    "element_key_attr": attr_name,
                    "element_key_value": attr_val,
                })
            else:
                # Both have this element — compare attrs then recurse
                _diff_attrs(main_child, cust_child, modifications, seen, child_xpath)
                _diff_children(main_child, cust_child, child_xpath, modifications, seen)

        # --- Positional elements (no identity key) ---
        min_len = min(len(main_positional), len(cust_positional))

        # Matched positional elements
        for i in range(min_len):
            child_xpath = parent_xpath + "/" + tag + "[" + str(i + 1) + "]"
            _diff_attrs(main_positional[i], cust_positional[i], modifications, seen, child_xpath)
            _diff_children(main_positional[i], cust_positional[i], child_xpath, modifications, seen)

        # Extra positional elements in customer (added)
        for i in range(min_len, len(cust_positional)):
            modifications.append({
                "action": "add_element",
                "parent_xpath": parent_xpath,
                "xml": ET.tostring(cust_positional[i], encoding="unicode"),
            })

        # Extra positional elements in main (removed in customer)
        for i in range(min_len, len(main_positional)):
            modifications.append({
                "action": "remove_element",
                "parent_xpath": parent_xpath,
                "element_tag": tag,
                "element_attrs": dict(main_positional[i].attrib),
            })


# ---------------------------------------------------------------------------
# apply-diff
# ---------------------------------------------------------------------------

def apply_diff(twbx_name: Optional[str] = None) -> None:
    """Rebuild each customer .twbx as: copy of Main + contract.json modifications.

    For every matching main/customer pair (or just the one named by twbx_name):
      1. Copy Main into a temp directory.
      2. Look up this file's entry in contract.json (empty list if absent or no entry).
      3. Apply the contract modifications to the copy.
      4. Write the result to the customer path.

    contract.json is optional — if it does not exist the customer becomes a plain
    copy of Main.

    Args:
        twbx_name: If given, process only this filename; otherwise process all pairs.
    """
    pairs = find_twbx_pairs()
    if not pairs:
        print("No matching .twbx pairs found in Live/ and Live/Customers/.", file=sys.stderr)
        sys.exit(1)

    # Load contract (tolerates missing file).
    contract_path = CUSTOMERS_DIR / DIFFERENCES_FILE
    contract: Dict = {}
    if contract_path.exists():
        contract = json.loads(contract_path.read_text(encoding="utf-8"))

    any_processed = False
    for main_path, customer_path in pairs:
        if twbx_name and main_path.name != twbx_name:
            continue

        print("Rebuilding: " + str(customer_path.relative_to(REPO_ROOT)))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            main_extract = tmp_dir / "main"
            output_dir = tmp_dir / "output"

            main_twb = extract_twb(main_path, main_extract)
            shutil.copytree(str(main_extract), str(output_dir))
            output_twb = output_dir / main_twb.relative_to(main_extract)

            # Retrieve modifications from contract (may be absent = no customisations).
            file_contract = contract.get(main_path.name, {})
            xml_changes = file_contract.get("xml_changes", {})
            total_mods = 0
            for _twb_file, change_data in xml_changes.items():
                modifications = change_data.get("xpath_modifications", [])
                if modifications:
                    tree = ET.parse(output_twb)
                    applied = _apply_modifications(tree, modifications)
                    _buf = io.BytesIO()
                    tree.write(_buf, encoding="utf-8", xml_declaration=True)
                    output_twb.write_bytes(_buf.getvalue())
                    total_mods += applied

            repack_twbx(output_dir, customer_path)
            print("  Applied " + str(total_mods) + " modification(s) -> saved " + customer_path.name)
        any_processed = True

    if not any_processed and twbx_name:
        print("File not found in pairs: " + twbx_name, file=sys.stderr)


def _find_parent(root: ET.Element, parent_xpath: str) -> Optional[ET.Element]:
    """Locate the parent element by XPath. '.' means root itself."""
    if parent_xpath == ".":
        return root
    return root.find(parent_xpath)


def _apply_modifications(tree: ET.ElementTree, modifications: List[Dict]) -> int:
    """Apply xpath_modifications to tree in-place. Returns number of changes made."""
    root = tree.getroot()
    change_count = 0

    for mod in modifications:
        action = mod.get("action")

        # ------------------------------------------------------------------
        # change_attribute
        # ------------------------------------------------------------------
        if action == "change_attribute":
            tag = mod["tag"]
            attribute = mod["attribute"]
            old_value = mod["old_value"]
            new_value = mod["new_value"]
            attr_filter: Dict = mod.get("attr_filter", {})
            element_xpath = mod.get("element_xpath", "")

            if element_xpath:
                # Precise mode: target exactly one element by its full XPath
                target = root.find(element_xpath)
                if target is not None and target.attrib.get(attribute) == old_value:
                    target.set(attribute, new_value)
                    change_count += 1
            else:
                # Legacy / global mode: change all matching elements
                for elem in root.iter(tag):
                    if all(elem.attrib.get(k) == v for k, v in attr_filter.items()):
                        if elem.attrib.get(attribute) == old_value:
                            elem.set(attribute, new_value)
                            change_count += 1

        # ------------------------------------------------------------------
        # set_attribute  (unconditional set, no old_value check)
        # ------------------------------------------------------------------
        elif action == "set_attribute":
            tag = mod["tag"]
            attribute = mod["attribute"]
            new_value = mod["new_value"]
            attr_filter = mod.get("attr_filter", {})
            for elem in root.iter(tag):
                if all(elem.attrib.get(k) == v for k, v in attr_filter.items()):
                    elem.set(attribute, new_value)
                    change_count += 1

        # ------------------------------------------------------------------
        # delete_attribute
        # ------------------------------------------------------------------
        elif action == "delete_attribute":
            tag = mod["tag"]
            attribute = mod["attribute"]
            attr_filter = mod.get("attr_filter", {})
            for elem in root.iter(tag):
                if all(elem.attrib.get(k) == v for k, v in attr_filter.items()):
                    if attribute in elem.attrib:
                        del elem.attrib[attribute]
                        change_count += 1

        # ------------------------------------------------------------------
        # add_element  — append serialised XML child to a parent
        # ------------------------------------------------------------------
        elif action == "add_element":
            parent_xpath = mod.get("parent_xpath", ".")
            xml_str = mod.get("xml", "")
            parent = _find_parent(root, parent_xpath)
            if parent is None:
                print(
                    "  WARNING: parent not found for add_element: " + parent_xpath,
                    file=sys.stderr,
                )
                continue
            try:
                new_elem = ET.fromstring(xml_str)
                # Skip if an element with the same identity key already exists in parent.
                # This prevents duplicates when the same element was added by a prior propagation.
                key = _get_key(new_elem)
                if key:
                    attr_name, attr_val = key
                    safe_val = attr_val.replace("'", "\\'")
                    if parent.find(new_elem.tag + "[@" + attr_name + "='" + safe_val + "']") is not None:
                        continue
                parent.append(new_elem)
                change_count += 1
            except ET.ParseError as exc:
                print("  WARNING: could not parse add_element xml: " + str(exc), file=sys.stderr)

        # ------------------------------------------------------------------
        # remove_element  — remove a child element identified by key attr or full attrs
        # ------------------------------------------------------------------
        elif action == "remove_element":
            parent_xpath = mod.get("parent_xpath", ".")
            element_tag = mod.get("element_tag", "")
            parent = _find_parent(root, parent_xpath)
            if parent is None:
                print(
                    "  WARNING: parent not found for remove_element: " + parent_xpath,
                    file=sys.stderr,
                )
                continue

            for child in list(parent):
                if child.tag != element_tag:
                    continue
                if "element_key_attr" in mod:
                    if child.get(mod["element_key_attr"]) == mod["element_key_value"]:
                        parent.remove(child)
                        change_count += 1
                        break
                else:
                    elem_attrs: Dict = mod.get("element_attrs", {})
                    if all(child.get(k) == v for k, v in elem_attrs.items()):
                        parent.remove(child)
                        change_count += 1
                        break

    return change_count


# ---------------------------------------------------------------------------
# propagate — apply delta(old Main → new Main) to Customer in-place
# ---------------------------------------------------------------------------

def _is_layout_structural(change: Dict) -> bool:
    """Return True if this change is a structural add/remove of a Tableau layout element.

    Tableau regenerates zone/pane/window IDs on every save, so propagating
    these structural changes would duplicate elements that already exist in
    the customer file with the new IDs.
    """
    if change["action"] not in ("add_element", "remove_element"):
        return False
    if change.get("element_tag", "") in _TABLEAU_LAYOUT_TAGS:
        return True
    xml = change.get("xml", "").strip()
    return any(
        xml.startswith("<" + t + " ") or xml.startswith("<" + t + ">")
        for t in _TABLEAU_LAYOUT_TAGS
    )

def _apply_changes_to_customer(customer_path: Path, changes: List[Dict]) -> int:
    """Apply a list of xpath_modifications to an existing customer .twbx in-place.

    Unlike apply_diff (which rebuilds customer from main), this modifies the
    customer file directly so that customer-specific elements (e.g. extra measures)
    are preserved.  Returns the number of individual XML changes applied.
    """
    # #region agent log
    import json as _json, time as _time
    _log_path = REPO_ROOT / "debug-ae1ef6.log"
    def _dbg(msg, data, hyp=""):
        with open(str(_log_path), "a", encoding="utf-8") as _f:
            _f.write(_json.dumps({"sessionId":"ae1ef6","timestamp":int(_time.time()*1000),"location":"tableau_sync.py:_apply_changes_to_customer","message":msg,"data":data,"hypothesisId":hyp}) + "\n")
    # #endregion

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        cust_extract = tmp_dir / "cust"
        cust_twb = extract_twb(customer_path, cust_extract)

        # #region agent log
        import xml.etree.ElementTree as _ET2
        _cust_tree_pre = _ET2.parse(str(cust_twb))
        _cust_root_pre = _cust_tree_pre.getroot()
        _zone_ids_before = [z.get("id") for z in _cust_root_pre.iter("zone")]
        _pane_ids_before = [p.get("id") for p in _cust_root_pre.iter("pane")]
        _dbg("customer_ids_before_apply", {"zone_ids": _zone_ids_before, "pane_ids": _pane_ids_before, "total_zones": len(_zone_ids_before), "changes_count": len(changes)}, "H1-H3")
        # #endregion

        tree = ET.parse(cust_twb)
        applied = _apply_modifications(tree, changes)
        _buf = io.BytesIO()
        tree.write(_buf, encoding="utf-8", xml_declaration=True)
        cust_twb.write_bytes(_buf.getvalue())

        # #region agent log
        _cust_tree_post = _ET2.parse(str(cust_twb))
        _cust_root_post = _cust_tree_post.getroot()
        _zone_ids_after = [z.get("id") for z in _cust_root_post.iter("zone")]
        _dup_zone_ids = [zid for zid in _zone_ids_after if _zone_ids_after.count(zid) > 1]
        _dbg("customer_ids_after_apply", {"zone_ids": _zone_ids_after, "duplicate_zone_ids": list(set(_dup_zone_ids)), "total_zones": len(_zone_ids_after), "applied": applied}, "H1-H2")
        # #endregion

        repack_twbx(cust_extract, customer_path)
    return applied


def propagate(twbx_name: Optional[str] = None) -> None:
    """Propagate changes from Main to Customer by diffing old vs new Main.

    For each .twbx pair:
      1. Retrieve the previous committed version of Main from git HEAD.
      2. Compute delta = _diff_trees(old Main, new Main).
      3. Apply only those changes to Customer in-place, preserving anything
         unique to Customer (extra measures, columns, custom panes, etc.).

    Args:
        twbx_name: If given, process only this filename; otherwise process all pairs.
    """
    # #region agent log
    import json as _json, time as _time
    _log_path = REPO_ROOT / "debug-ae1ef6.log"
    def _dbg(msg, data, hyp=""):
        with open(str(_log_path), "a", encoding="utf-8") as _f:
            _f.write(_json.dumps({"sessionId":"ae1ef6","timestamp":int(_time.time()*1000),"location":"tableau_sync.py:propagate","message":msg,"data":data,"hypothesisId":hyp}) + "\n")
    # #endregion

    pairs = find_twbx_pairs()
    if not pairs:
        print("No matching .twbx pairs found in Live/ and Live/Customers/.", file=sys.stderr)
        sys.exit(1)

    any_processed = False
    for main_path, customer_path in pairs:
        if twbx_name and main_path.name != twbx_name:
            continue

        git_path = "Live/" + main_path.name
        old_result = subprocess.run(
            ["git", "show", "HEAD:" + git_path],
            cwd=REPO_ROOT,
            capture_output=True,
        )

        if old_result.returncode != 0 or not old_result.stdout:
            print(
                "No previous version of " + main_path.name + " in HEAD; skipping propagation.",
                file=sys.stderr,
            )
            continue

        print("Propagating: " + main_path.name + " -> " + customer_path.name + " ...")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            old_twb = extract_twb_from_bytes(old_result.stdout, tmp_dir / "old")
            new_twb = extract_twb(main_path, tmp_dir / "new")
            changes = _diff_trees(ET.parse(old_twb), ET.parse(new_twb))

        # #region agent log
        add_mods  = [c for c in changes if c["action"] == "add_element"]
        rem_mods  = [c for c in changes if c["action"] == "remove_element"]
        chg_mods  = [c for c in changes if c["action"] == "change_attribute"]
        zone_adds = [c for c in add_mods  if "<zone " in c.get("xml","") or "<zone>" in c.get("xml","")]
        zone_rems = [c for c in rem_mods  if c.get("element_tag") == "zone"]
        zone_chgs = [c for c in chg_mods  if c.get("tag") == "zone"]
        id_chgs   = [c for c in chg_mods  if c.get("attribute") == "id"]
        _dbg("computed_delta", {
            "total_changes": len(changes),
            "add_element": len(add_mods),
            "remove_element": len(rem_mods),
            "change_attribute": len(chg_mods),
            "zone_adds": len(zone_adds),
            "zone_removes": len(zone_rems),
            "zone_attr_changes": len(zone_chgs),
            "id_attr_changes": len(id_chgs),
            "id_changes_sample": [{"tag":c.get("tag"),"old":c.get("old_value"),"new":c.get("new_value")} for c in id_chgs[:5]],
            "zone_adds_sample": [c.get("xml","")[:120] for c in zone_adds[:3]],
        }, "H1-H2-H3-H5")

        # Filter out layout-structural changes. Tableau regenerates zone/pane IDs on
        # every save, so add_element/remove_element for those tags would duplicate
        # elements that already exist in Customer.
        changes = [c for c in changes if not _is_layout_structural(c)]

        if not changes:
            print("  No changes detected; Customer is already up to date.")
            any_processed = True
            continue

        attr_ch = sum(1 for c in changes if c["action"] == "change_attribute")
        adds = sum(1 for c in changes if c["action"] == "add_element")
        removes = sum(1 for c in changes if c["action"] == "remove_element")
        print(
            "  -> "
            + str(attr_ch) + " attribute change(s), "
            + str(adds) + " element addition(s), "
            + str(removes) + " element removal(s)."
        )

        applied = _apply_changes_to_customer(customer_path, changes)
        print("  Applied " + str(applied) + " modification(s) -> saved " + customer_path.name)
        any_processed = True

    if not any_processed and twbx_name:
        print("File not found in pairs: " + twbx_name, file=sys.stderr)


# ---------------------------------------------------------------------------
# pre-commit
# ---------------------------------------------------------------------------

def pre_commit() -> None:
    """Git pre-commit hook: rebuild each staged customer .twbx as copy(main) + contract."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("git diff failed: " + result.stderr, file=sys.stderr)
        sys.exit(1)

    staged_files = result.stdout.strip().splitlines()

    # Only files directly in Live/ (not in sub-directories like Live/Customers/)
    main_staged = [
        f for f in staged_files
        if f.startswith("Live/") and f.endswith(".twbx") and f.count("/") == 1
    ]

    if not main_staged:
        sys.exit(0)

    any_synced = False
    for staged_path in main_staged:
        twbx_name = Path(staged_path).name
        customer_path = CUSTOMERS_DIR / twbx_name

        if not customer_path.exists():
            print("Customer file not found for " + twbx_name + "; skipping.", file=sys.stderr)
            continue

        apply_diff(twbx_name)

        customer_rel = "Live/Customers/" + twbx_name
        add_result = subprocess.run(
            ["git", "add", customer_rel],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if add_result.returncode != 0:
            print("git add failed: " + add_result.stderr, file=sys.stderr)
            sys.exit(1)
        print("  Staged " + customer_rel)
        any_synced = True

    if any_synced:
        print("Tableau sync complete.")
    sys.exit(0)


# ---------------------------------------------------------------------------
# repair — remove duplicate layout elements from customer files
# ---------------------------------------------------------------------------

def repair(twbx_name: Optional[str] = None) -> None:
    """Remove duplicate zone/pane/window elements from customer .twbx files.

    Previous propagation runs may have added duplicate layout elements.
    This command finds every <zones> / <panes> parent and removes later
    duplicate children that share an id attribute with an earlier sibling.

    Args:
        twbx_name: If given, repair only this customer file; otherwise all.
    """
    pairs = find_twbx_pairs()
    if not pairs:
        print("No matching .twbx pairs found.", file=sys.stderr)
        sys.exit(1)

    for main_path, customer_path in pairs:
        if twbx_name and main_path.name != twbx_name:
            continue

        print("Repairing: " + str(customer_path.relative_to(REPO_ROOT)))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            cust_extract = tmp_dir / "cust"
            cust_twb = extract_twb(customer_path, cust_extract)

            tree = ET.parse(cust_twb)
            root = tree.getroot()
            removed = 0

            for parent in root.iter():
                seen_ids: Dict[str, ET.Element] = {}
                for child in list(parent):
                    if child.tag not in _TABLEAU_LAYOUT_TAGS:
                        continue
                    child_id = child.get("id")
                    if child_id is None:
                        continue
                    key = child.tag + ":" + child_id
                    if key in seen_ids:
                        parent.remove(child)
                        removed += 1
                    else:
                        seen_ids[key] = child

            _buf = io.BytesIO()
            tree.write(_buf, encoding="utf-8", xml_declaration=True)
            cust_twb.write_bytes(_buf.getvalue())
            repack_twbx(cust_extract, customer_path)
            print("  Removed " + str(removed) + " duplicate layout element(s) -> saved " + customer_path.name)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tableau .twbx sync -- generate-diff / apply-diff / repair / pre-commit"
    )
    parser.add_argument(
        "command",
        choices=["generate-diff", "apply-diff", "repair", "pre-commit"],
    )
    parser.add_argument(
        "--file",
        metavar="NAME.twbx",
        help="Process a single .twbx file by name (apply-diff / repair only)",
    )
    args = parser.parse_args()

    if args.command == "generate-diff":
        generate_diff()
    elif args.command == "apply-diff":
        apply_diff(args.file)
    elif args.command == "repair":
        repair(args.file)
    elif args.command == "pre-commit":
        pre_commit()


if __name__ == "__main__":
    main()
