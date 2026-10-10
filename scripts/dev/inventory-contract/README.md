# Inventory API contract generator

Generates `Documentation/Inventory/05-P1-API-CONTRACT.md`, the contract the Inventory dashboard is built against, and checks it.
The document is **generated**: never edit it by hand. Edit the template (prose) or the code it reads, then rebuild.

## What it needs

* Python with the server's requirements (`pip install -r server/requirements.txt`; CI uses the same interpreter as the server tests).
* Nothing else. No database, no network, no running service, no environment variables, no path outside this repository.
  (`facts.py` and `check_contract.py` import `server/app/...`; the modules only touch the environment when a request runs.)

Run everything from the repository root.

## Rebuild the document

```
python scripts/dev/inventory-contract/build_contract.py            # rewrite Documentation/Inventory/05-P1-API-CONTRACT.md
python scripts/dev/inventory-contract/build_contract.py --check    # exit 1 (and print a diff) when the committed file is stale
python scripts/dev/inventory-contract/build_contract.py --out X.md # write somewhere else
```

The output is deterministic: two runs give identical bytes (`--check` after a build proves it).

Rebuild after any of: a change to `contract.template.md`, a constant in the server code that the document states (limits, enums,
defaults, export columns), a regenerated golden fixture (`ZENPLUS_UPDATE_FIXTURES=1 python -m pytest server/tests/test_inventory_fixtures.py`),
or an edit of a performance partial.

## Inputs

| File | What it is |
|---|---|
| `contract.template.md` | The hand-written prose, TypeScript interfaces, rules, the drill-down map and the definitions, with placeholders. |
| `examples.py` | Cuts the golden fixtures (`dashboard/src/pages/inventory/fixtures/*.json`) into the JSON examples. A cut only drops list items; it never edits a value. `server/tests/test_inventory_fixtures.py::test_every_json_example_of_the_contract_is_a_real_response_cut_short` proves every example in the document is a cut of its fixture. |
| `facts.py` | Imports the limits, enums, defaults and column lists from `server/app` (`@@F:name@@`), so the document cannot keep describing a value the code has changed. `python scripts/dev/inventory-contract/facts.py` lists them. |
| `perf-core.md`, `perf-software.md` | Section 6.5, measured performance. **Static text, not regenerated**: the numbers come from the harnesses of the work that measured them (scratch PostgreSQL 16 fleets of 5,000 servers x 400 packages, 10,000 devices, 1 M events; the harness scripts are not in this repository). Edit them by hand when a measurement is repeated, and say what the fleet was. They may contain `@@F:...@@` placeholders. |

### Placeholders in `contract.template.md`

| Placeholder | Replaced by |
|---|---|
| `@@EX:<name>@@` | The JSON example built by `examples.py` (`summary`, `assets`, `facets`, `asset-server`, `asset-device`, `asset-device-ha`, `os`, `hardware`, `changes`, `software`, `software-publishers`, `software-product`, `software-installations`, `trends`, `settings`). The test fixes their order in the document. |
| `@@F:<name>@@` | A fact from `facts.py` (a number, or a list of backticked values). |
| `@@G:<fixture>.<path>@@` | A plain value read from a golden file, for example `@@G:software.total@@` or `@@G:index.pinned_now@@`; a numeric path segment indexes a list and `#len` at the end gives a length. Use it for every number that describes the fixture fleet. |
| `@@PERF@@`, `@@PERF_SW@@` | The contents of `perf-core.md` and `perf-software.md`. |

A placeholder that names nothing is an error, and so is one left over after the build. `examples.py` picks some rows by name (for
example `edge-unk-01`); when a regenerated fixture no longer holds a picked row the build stops with a `KeyError` naming it: choose
another row of the same kind and adjust the caption in the template.

## Check the document against the code

```
python scripts/dev/inventory-contract/check_contract.py [path-to-a-copy-of-the-document]
```

Exits 1 with a list of problems when the document disagrees with the code or the tree:

1. every route under `/api/v1/inventory` has a `### 3.N` heading;
2. every query parameter of a route is named in its section (or in section 2 for the asset filters) and every parameter named in a section's
   parameter table is one the route takes;
3. the values listed for a `Literal` query parameter are the route's values;
4. every TypeScript `interface` of the document has exactly the fields, nested objects and nullability of the pydantic response model of
   the same name (`server/app/schemas/inventory.py`);
5. the extra parameters of `GET /devices` and the extra fields of `GET /servers/{id}/software` described in section 3.15 exist;
6. every repository path and file name in backticks exists, every `/inventory/...` page route is a route of the dashboard
   (`navigation.ts`), every API path is a route;
7. every "section N" and "doc 02 section N" reference points at an existing heading.

It complements, and does not replace, the tests that already guard the document
(`server/tests/test_inventory_fixtures.py`: the JSON examples are cuts of real responses, every route, filter, sort and health key is named;
`server/tests/test_inventory_api.py`: every number of the drill-down map equals the total of the list behind it).

## In CI

Neither command needs a service, so they can run next to the server tests (the workflow files are not part of this directory):

```
python scripts/dev/inventory-contract/build_contract.py --check
python scripts/dev/inventory-contract/check_contract.py
```

## Typical session

```
# a service constant or a response shape changed
ZENPLUS_UPDATE_FIXTURES=1 python -m pytest server/tests/test_inventory_fixtures.py   # only for a response change; review the diff
python scripts/dev/inventory-contract/build_contract.py
python scripts/dev/inventory-contract/check_contract.py
python -m pytest server/tests/test_inventory_fixtures.py
```
