---
name: tableau-sync
description: >-
  Manage the Tableau .twbx sync system for this repo: run generate-diff to
  capture customer-specific XML attribute overrides, install the git pre-commit
  hook, apply diffs manually, or troubleshoot sync issues. Use when the user
  mentions tableau_sync, differences.json, twbx sync, Tableau customer files,
  or asks to set up / reset / debug the Tableau transform pipeline.
disable-model-invocation: true
---

# Tableau Sync

Syncs `Live/Sales.twbx` (main) → `Live/Customers/Sales.twbx` (customer) via a
delta in `Live/Customers/differences.json`.

## Key files

| File | Role |
|------|------|
| `scripts/tableau_sync.py` | Core logic — three modes below |
| `scripts/install_hooks.py` | One-time hook installer |
| `Live/Customers/differences.json` | Customer-specific XML attribute overrides |
| `.git/hooks/pre-commit` | Auto-sync on every commit that touches a main `.twbx` |

---

## Workflows

### First-time setup (new clone / after hook removal)

```
python scripts/install_hooks.py
python scripts/tableau_sync.py generate-diff
```

Review `Live/Customers/differences.json` and edit `new_value` fields to set
the correct customer-specific values, then run `apply-diff` once to rebuild.

### generate-diff — detect attribute changes

```
python scripts/tableau_sync.py generate-diff
```

- Compares every `Live/*.twbx` with its `Live/Customers/*.twbx` counterpart.
- Finds attributes whose **value was swapped** (1-to-1 replacement) between
  main and customer.
- Writes `Live/Customers/differences.json`.

> Note: structural differences (added/removed elements) are not captured — only
> attribute **value replacements** appear in `xpath_modifications`.

### apply-diff — rebuild customer file

```
python scripts/tableau_sync.py apply-diff              # all files
python scripts/tableau_sync.py apply-diff --file X.twbx  # one file
```

- Reads `differences.json`.
- Extracts the `.twb` XML from the main `.twbx`.
- Applies each `xpath_modification` entry.
- Repacks as `Live/Customers/X.twbx`.

### pre-commit hook (automatic)

Fires automatically when `git commit` includes a staged `Live/*.twbx` file.
Calls `apply-diff` for each affected file, then `git add`s the updated customer
file so both are committed together.

---

## differences.json format

```json
{
  "Sales.twbx": {
    "description": "...",
    "xml_changes": {
      "Sales.twb": {
        "xpath_modifications": [
          {
            "action": "change_attribute",
            "tag": "format",
            "attribute": "value",
            "old_value": "#1b1b1b",
            "new_value": "#customer_dark",
            "attr_filter": {}
          }
        ]
      }
    }
  }
}
```

### Supported actions

| Action | Required fields | Effect |
|--------|----------------|--------|
| `change_attribute` | `tag`, `attribute`, `old_value`, `new_value` | Replace attribute value |
| `set_attribute` | `tag`, `attribute`, `new_value` | Set attribute unconditionally |
| `delete_attribute` | `tag`, `attribute` | Remove attribute |

`attr_filter` (optional on all actions): `{ "otherAttr": "requiredValue" }` —
only elements that also match these attributes are modified.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Hook doesn't fire | Run `python scripts/install_hooks.py` again |
| 0 modifications after generate-diff | Files differ structurally, not by value swaps — edit `differences.json` manually |
| Customer file not updated after commit | Check `git log --oneline -1` includes `Live/Customers/` changes |
| Unicode error on Windows console | Already fixed in `tableau_sync.py` — no action needed |
