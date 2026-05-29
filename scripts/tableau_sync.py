#!/usr/bin/env python3
"""tableau_sync.py - Synchronise Tableau .twbx files between main (Live/) and customer (Live/Customers/) versions.

Usage:
    python scripts/tableau_sync.py generate-diff             # First time: create differences.json
    python scripts/tableau_sync.py apply-diff                # Update customer file from main + delta
    python scripts/tableau_sync.py apply-diff --file X.twbx  # Process one specific file
    python scripts/tableau_sync.py pre-commit                # Called by git pre-commit hook
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_DIR = REPO_ROOT / "Live"
CUSTOMERS_DIR = LIVE_DIR / "Customers"
DIFFERENCES_FILE = "differences.json"


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
        raise FileNotFoundError(f"No .twb file found inside {twbx_path}")
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
    """Compare each main .twbx with its customer counterpart and write differences.json."""
    pairs = find_twbx_pairs()
    if not pairs:
        print("No matching .twbx pairs found in Live/ and Live/Customers/.", file=sys.stderr)
        sys.exit(1)

    all_diffs: Dict = {}

    for main_path, customer_path in pairs:
        print(f"Comparing: {main_path.relative_to(REPO_ROOT)}  vs  {customer_path.relative_to(REPO_ROOT)}")

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
            print(f"  -> {len(modifications)} modification(s) found.")

            all_diffs[main_path.name] = {
                "description": (
                    f"Differences between main {main_path.name} and customer {customer_path.name}"
                ),
                "xml_changes": {
                    twb_filename: {
                        "xpath_modifications": modifications,
                    }
                },
            }

    out_path = CUSTOMERS_DIR / DIFFERENCES_FILE
    out_path.write_text(json.dumps(all_diffs, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {out_path.relative_to(REPO_ROOT)}")


def _diff_trees(main_tree: ET.ElementTree, cust_tree: ET.ElementTree) -> List[Dict]:
    """Walk both XML trees and return a list of change_attribute modifications.

    Strategy: collect every (tag, attr) → set-of-values from each tree, then
    record 1-to-1 value replacements.  Many-to-many differences are also
    recorded as individual pairs (best-effort).
    """
    def collect(tree: ET.ElementTree) -> Dict:
        data: Dict = {}
        for elem in tree.iter():
            for attr, val in elem.attrib.items():
                data.setdefault((elem.tag, attr), set()).add(val)
        return data

    main_attrs = collect(main_tree)
    cust_attrs = collect(cust_tree)

    modifications: List = []
    seen: Set = set()

    for (tag, attr), cust_vals in cust_attrs.items():
        main_vals = main_attrs.get((tag, attr), set())
        added = cust_vals - main_vals    # values present in customer but not main
        removed = main_vals - cust_vals  # values present in main but not customer

        if not added or not removed:
            continue

        if len(added) == 1 and len(removed) == 1:
            old_v = next(iter(removed))
            new_v = next(iter(added))
            key = (tag, attr, old_v, new_v)
            if key not in seen:
                seen.add(key)
                modifications.append({
                    "action": "change_attribute",
                    "tag": tag,
                    "attribute": attr,
                    "old_value": old_v,
                    "new_value": new_v,
                    "attr_filter": {},
                })
        else:
            for old_v in sorted(removed):
                for new_v in sorted(added):
                    key = (tag, attr, old_v, new_v)
                    if key not in seen:
                        seen.add(key)
                        modifications.append({
                            "action": "change_attribute",
                            "tag": tag,
                            "attribute": attr,
                            "old_value": old_v,
                            "new_value": new_v,
                            "attr_filter": {},
                        })

    return modifications


# ---------------------------------------------------------------------------
# apply-diff
# ---------------------------------------------------------------------------

def apply_diff(twbx_name: Optional[str] = None) -> None:
    """Read differences.json and regenerate customer .twbx file(s) from main + delta.

    Args:
        twbx_name: If given, process only this filename; otherwise process all entries.
    """
    diff_file = CUSTOMERS_DIR / DIFFERENCES_FILE
    if not diff_file.exists():
        print(f"differences.json not found at {diff_file}", file=sys.stderr)
        sys.exit(1)

    all_diffs: Dict = json.loads(diff_file.read_text(encoding="utf-8"))

    for twbx_file_name, diff_data in all_diffs.items():
        if twbx_name and twbx_file_name != twbx_name:
            continue

        main_path = LIVE_DIR / twbx_file_name
        customer_path = CUSTOMERS_DIR / twbx_file_name

        if not main_path.exists():
            print(f"Main file not found: {main_path.relative_to(REPO_ROOT)}", file=sys.stderr)
            continue

        print(f"Applying diff -> {customer_path.relative_to(REPO_ROOT)}")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            main_extract = tmp_dir / "main"
            output_dir = tmp_dir / "output"

            main_twb = extract_twb(main_path, main_extract)

            # Start from a copy of the main extraction
            shutil.copytree(str(main_extract), str(output_dir))
            output_twb = output_dir / main_twb.relative_to(main_extract)

            xml_changes = diff_data.get("xml_changes", {})
            total_mods = 0
            for _twb_file, change_data in xml_changes.items():
                modifications = change_data.get("xpath_modifications", [])
                if modifications:
                    tree = ET.parse(output_twb)
                    applied = _apply_modifications(tree, modifications)
                    tree.write(
                        str(output_twb),
                        encoding="unicode",
                        xml_declaration=False,
                    )
                    total_mods += applied

            repack_twbx(output_dir, customer_path)
            print(f"  Applied {total_mods} modification(s) -> saved {customer_path.name}")


def _apply_modifications(tree: ET.ElementTree, modifications: list) -> int:
    """Apply xpath_modifications to tree in-place. Returns number of element changes made."""
    root = tree.getroot()
    change_count = 0

    for mod in modifications:
        action = mod.get("action")
        tag = mod["tag"]
        attr_filter: dict = mod.get("attr_filter", {})

        if action == "change_attribute":
            attribute = mod["attribute"]
            old_value = mod["old_value"]
            new_value = mod["new_value"]
            for elem in root.iter(tag):
                if all(elem.attrib.get(k) == v for k, v in attr_filter.items()):
                    if elem.attrib.get(attribute) == old_value:
                        elem.set(attribute, new_value)
                        change_count += 1

        elif action == "set_attribute":
            attribute = mod["attribute"]
            new_value = mod["new_value"]
            for elem in root.iter(tag):
                if all(elem.attrib.get(k) == v for k, v in attr_filter.items()):
                    elem.set(attribute, new_value)
                    change_count += 1

        elif action == "delete_attribute":
            attribute = mod["attribute"]
            for elem in root.iter(tag):
                if all(elem.attrib.get(k) == v for k, v in attr_filter.items()):
                    if attribute in elem.attrib:
                        del elem.attrib[attribute]
                        change_count += 1

    return change_count


# ---------------------------------------------------------------------------
# pre-commit
# ---------------------------------------------------------------------------

def pre_commit() -> None:
    """Git pre-commit hook: sync staged main .twbx files to Live/Customers/ and re-stage them."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"git diff failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    staged_files = result.stdout.strip().splitlines()

    # Only files directly in Live/ (not in any sub-directory like Live/Customers/)
    main_staged = [
        f for f in staged_files
        if f.startswith("Live/") and f.endswith(".twbx") and f.count("/") == 1
    ]

    if not main_staged:
        sys.exit(0)

    diff_file = CUSTOMERS_DIR / DIFFERENCES_FILE
    if not diff_file.exists():
        print("No differences.json found; skipping Tableau sync.", file=sys.stderr)
        sys.exit(0)

    all_diffs: Dict = json.loads(diff_file.read_text(encoding="utf-8"))

    any_synced = False
    for staged_path in main_staged:
        twbx_name = Path(staged_path).name
        if twbx_name not in all_diffs:
            print(f"No diff entry for {twbx_name}; skipping.", file=sys.stderr)
            continue

        print(f"Syncing {twbx_name} -> Live/Customers/{twbx_name} ...")
        apply_diff(twbx_name)

        customer_rel = f"Live/Customers/{twbx_name}"
        add_result = subprocess.run(
            ["git", "add", customer_rel],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if add_result.returncode != 0:
            print(f"git add failed: {add_result.stderr}", file=sys.stderr)
            sys.exit(1)
        print(f"  Staged {customer_rel}")
        any_synced = True

    if any_synced:
        print("Tableau sync complete.")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tableau .twbx sync — generate-diff / apply-diff / pre-commit"
    )
    parser.add_argument(
        "command",
        choices=["generate-diff", "apply-diff", "pre-commit"],
    )
    parser.add_argument(
        "--file",
        metavar="NAME.twbx",
        help="Process a single .twbx file by name (apply-diff only)",
    )
    args = parser.parse_args()

    if args.command == "generate-diff":
        generate_diff()
    elif args.command == "apply-diff":
        apply_diff(args.file)
    elif args.command == "pre-commit":
        pre_commit()


if __name__ == "__main__":
    main()
