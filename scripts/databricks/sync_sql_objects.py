# Databricks notebook source
# MAGIC %md
# MAGIC # Copy functions and procedures from one environment to another
# MAGIC
# MAGIC Copies every user-defined function (scalar, inline and multi-statement table functions) and
# MAGIC stored procedure of a schema, e.g. `dbo_professional360` from `dv` to `pp`, as they are
# MAGIC defined in the source database (`sys.sql_modules`), including those the BFF team wrote
# MAGIC there. Tables and data are not touched. One environment cannot reach the other, so it
# MAGIC goes through a file, in two runs of this notebook:
# MAGIC
# MAGIC 1. **`action = export`** in the source workspace (`env = dv`): writes `file` (JSON, the
# MAGIC    objects in dependency order with the tables each reads) and the same name with `.sql`
# MAGIC    (a script runnable with `run_sql_script` or SSMS). Download the JSON file.
# MAGIC 2. Upload it in the target workspace.
# MAGIC 3. **`action = import`** there (`env = pp`), `mode = dry_run`: lists each object as new,
# MAGIC    changed or of another kind in the target, objects only in the target (never dropped) and
# MAGIC    tables the objects read that the target lacks (create them first, e.g.
# MAGIC    `UserNotificationRead` with `deploy/sql/bff_notifications.sql`). Then `mode = apply`:
# MAGIC    `CREATE OR ALTER` in dependency order, an object of another kind in the target dropped
# MAGIC    first (error 2010 otherwise), those already the same left alone; a rerun changes nothing.
# MAGIC
# MAGIC Permissions (`GRANT EXECUTE ...`) are not copied: principals differ between environments.
# MAGIC Connects with `azure_sql_connect` (D-057); guide: `docs/databricks-copy.md` section 3.

# COMMAND ----------

# MAGIC %pip install pyodbc pyyaml

# COMMAND ----------

# MAGIC %run ./azure_sql_connect

# COMMAND ----------

dbutils.widgets.dropdown("action", "export", ["export", "import"], "1. Action")
dbutils.widgets.dropdown("env", "dv", ["dv", "pp", "pr"], "2. Environment (export: source)")
dbutils.widgets.text("file", "/dbfs/FileStore/professional360/sql_objects.json", "3. File (JSON)")
dbutils.widgets.dropdown("mode", "dry_run", ["dry_run", "apply"], "4. Import mode")
dbutils.widgets.text("schema", "", "5. Schema (blank: azure.yaml app_schema)")
# Comma-separated object names; blank: all of them.
dbutils.widgets.text("only", "", "6. Only these objects (blank: all)")

# COMMAND ----------

import json
import re
from datetime import datetime, timezone
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

W = {k: dbutils.widgets.get(k).strip() for k in (
    "action", "env", "file", "mode", "schema", "only")}  # fmt: skip
S = azure_sql_settings(W["env"])
SCHEMA = W["schema"] or S.get("app_schema") or "dbo_professional360"
engine = sql_engine(S)
FILE = Path(W["file"])
assert FILE.suffix == ".json", "file: a .json path, e.g. /dbfs/FileStore/... or /Volumes/..."
ONLY = {n.strip() for n in W["only"].split(",") if n.strip()}
print(f"{W['action']}: {S['server']} / {S['database']} / {SCHEMA} ({W['env']}); file {FILE}")

KINDS = {"FN": "scalar function", "IF": "inline table function",
         "TF": "multi-statement table function", "P": "procedure"}  # fmt: skip
LEAD = r"(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*"  # blanks and comments before CREATE
HEAD = re.compile(rf"^({LEAD})CREATE\s+(?:OR\s+ALTER\s+)?(FUNCTION|PROCEDURE|PROC)\b",
                  re.IGNORECASE | re.DOTALL)  # fmt: skip


def modules(schema: str) -> dict[str, dict]:
    """Functions and procedures of `schema` here: name -> {object_id, name, type, definition}."""
    rows = query(engine, f"""
        SELECT o.object_id, o.name, RTRIM(o.type) AS type, m.definition
        FROM sys.objects o
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        LEFT JOIN sys.sql_modules m ON m.object_id = o.object_id
        WHERE s.name = N'{schema}' AND o.type IN ('FN', 'IF', 'TF', 'P')""")  # fmt: skip
    return {r["name"]: r for r in rows}


def create_or_alter(definition: str, src: str, dst: str) -> str:
    """A definition as CREATE OR ALTER, with schema `src` renamed to `dst`."""
    m = HEAD.match(definition)
    assert m, f"no CREATE FUNCTION / PROCEDURE at the start of:\n{definition[:300]}"
    sql = f"{m.group(1)}CREATE OR ALTER {m.group(2)}{definition[m.end() :]}"
    if src.lower() != dst.lower():
        sql = re.sub(rf"(\[{re.escape(src)}\]|\b{re.escape(src)}\b)\.", f"[{dst}].", sql,
                     flags=re.IGNORECASE)  # fmt: skip
    return sql


def same(a: str, b: str) -> bool:
    """Equal apart from line endings, trailing blanks and CREATE vs CREATE OR ALTER."""

    def norm(s: str) -> str:
        s = HEAD.sub(lambda m: f"{m.group(1)}CREATE {m.group(2).upper()}", s, count=1)
        return "\n".join(ln.rstrip() for ln in s.replace("\r\n", "\n").strip().splitlines())

    return norm(a) == norm(b)


def drop_other_kind(schema: str, name: str, type_: str) -> str:
    """T-SQL dropping `name` when it exists as another kind of object than `type_`."""
    oid = f"OBJECT_ID(N'[{schema}].[{name}]')"
    proc = f"IF EXISTS (SELECT 1 FROM sys.objects WHERE object_id = {oid} AND type = 'P') " \
           f"DROP PROCEDURE [{schema}].[{name}];"  # fmt: skip
    func = f"IF EXISTS (SELECT 1 FROM sys.objects WHERE object_id = {oid} " \
           f"AND type IN ('FN', 'IF', 'TF') AND type <> '{type_}') " \
           f"DROP FUNCTION [{schema}].[{name}];"  # fmt: skip
    return func if type_ == "P" else f"{proc}\n{func}"


# COMMAND ----------

# MAGIC %md ## Export (in the source environment)

# COMMAND ----------

if W["action"] == "export":
    source = modules(SCHEMA)
    if ONLY:
        unknown = ONLY - source.keys()
        assert not unknown, f"not in {SCHEMA}: {sorted(unknown)}"
        source = {n: r for n, r in source.items() if n in ONLY}
    hidden = sorted(n for n, r in source.items() if r["definition"] is None)
    assert not hidden, f"definitions not readable (encrypted or no VIEW DEFINITION): {hidden}"

    # Dependency order: a function before the functions and procedures that call it.
    ids = {r["object_id"]: n for n, r in source.items()}
    graph: dict[str, set[str]] = {n: set() for n in source}
    for d in query(
        engine,
        f"""
            SELECT d.referencing_id, d.referenced_id
            FROM sys.sql_expression_dependencies d
            JOIN sys.objects o ON o.object_id = d.referencing_id
            WHERE SCHEMA_NAME(o.schema_id) = N'{SCHEMA}' AND d.referenced_id IS NOT NULL""",
    ):
        a, b = ids.get(d["referencing_id"]), ids.get(d["referenced_id"])
        if a and b and a != b:
            graph[a].add(b)
    try:
        order = list(TopologicalSorter(graph).static_order())
    except CycleError:  # the import retries what fails on a later object
        order = sorted(source, key=lambda n: (source[n]["type"] == "P", n))

    # Tables and views each object reads (checked against the target by the import).
    reads: dict[str, set[tuple[str, str]]] = {n: set() for n in source}
    for r in query(
        engine,
        f"""
            SELECT DISTINCT o.name AS obj, ISNULL(d.referenced_schema_name, N'{SCHEMA}') AS sch,
                   d.referenced_entity_name AS tbl
            FROM sys.sql_expression_dependencies d
            JOIN sys.objects o ON o.object_id = d.referencing_id
            LEFT JOIN sys.objects r ON r.object_id = d.referenced_id
            WHERE SCHEMA_NAME(o.schema_id) = N'{SCHEMA}' AND d.referenced_database_name IS NULL
              AND (r.type IN ('U', 'V') OR r.object_id IS NULL)""",
    ):
        if r["obj"] in reads and r["tbl"] not in source:
            reads[r["obj"]].add((r["sch"], r["tbl"]))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")  # noqa: UP017 (DBR 3.10)
    objects = [{"name": n, "type": source[n]["type"], "definition": source[n]["definition"],
                "reads": sorted(reads[n])} for n in order]  # fmt: skip
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps({
        "exported_at": stamp, "env": W["env"], "server": S["server"],
        "database": S["database"], "schema": SCHEMA, "objects": objects,
    }, indent=1), encoding="utf-8")  # fmt: skip
    # The same as a script for run_sql_script or SSMS (no comparison, no table check).
    batches = [
        f"-- {len(objects)} functions and procedures of [{SCHEMA}], exported from "
        f"{W['env']} ({S['database']}) {stamp} by sync_sql_objects. Run in this order."
    ]
    for o in objects:
        batches.append(drop_other_kind(SCHEMA, o["name"], o["type"]))
        batches.append(create_or_alter(o["definition"], SCHEMA, SCHEMA).strip())
    FILE.with_suffix(".sql").write_text("\nGO\n\n".join(batches) + "\nGO\n", encoding="utf-8")
    counts = {KINDS[t]: sum(1 for o in objects if o["type"] == t) for t in KINDS}
    print(f"exported {len(objects)} objects {counts}")
    print(f"  {FILE}\n  {FILE.with_suffix('.sql')}")
    if str(FILE).startswith("/dbfs/FileStore/"):
        print(f"download: https://<this workspace>/files/{FILE.relative_to('/dbfs/FileStore')}")
    dbutils.notebook.exit(f"exported {len(objects)} objects to {FILE}")

# COMMAND ----------

# MAGIC %md ## Import (in the target environment): plan

# COMMAND ----------

assert W["action"] == "import"
assert FILE.is_file(), f"{FILE} not found: upload the exported file there"
exp = json.loads(FILE.read_text(encoding="utf-8"))
SRC = exp["schema"]
order = [o["name"] for o in exp["objects"] if not ONLY or o["name"] in ONLY]
source = {o["name"]: o for o in exp["objects"] if o["name"] in order}
if ONLY:
    unknown = ONLY - source.keys()
    assert not unknown, f"not in the file: {sorted(unknown)}"
assert (exp["server"], exp["database"], SRC) != (S["server"], S["database"], SCHEMA), (
    "the file was exported from this database and schema"
)
print(f"file: {len(source)} objects of {SRC} from {exp['env']} ({exp['database']}), "
      f"exported {exp['exported_at']}")  # fmt: skip
target = modules(SCHEMA)

plan: dict[str, str] = {}  # name -> new | changed | kind changed | same
for name, o in source.items():
    t = target.get(name)
    if t is None:
        plan[name] = "new"
    elif t["type"] != o["type"]:
        plan[name] = "kind changed"
    elif same(create_or_alter(o["definition"], SRC, SCHEMA), t["definition"] or ""):
        plan[name] = "same"
    else:
        plan[name] = "changed"

tables = query(engine, "SELECT SCHEMA_NAME(schema_id) AS sch, name FROM sys.objects "
                       "WHERE type IN ('U', 'V')")  # fmt: skip
have = {(r["sch"].lower(), r["name"].lower()) for r in tables}
missing: dict[str, set[str]] = {}
for name, o in source.items():
    for sch, tbl in o["reads"]:
        sch = SCHEMA if sch.lower() == SRC.lower() else sch
        if (sch.lower(), tbl.lower()) not in have:
            missing.setdefault(f"{sch}.{tbl}", set()).add(name)

counts = {k: sum(1 for v in plan.values() if v == k)
          for k in ("new", "changed", "kind changed", "same")}  # fmt: skip
print("plan:", counts)
for name in order:
    if plan[name] != "same":
        kind = KINDS[source[name]["type"]]
        extra = f" (here: {KINDS[target[name]['type']]})" if plan[name] == "kind changed" else ""
        print(f"  {plan[name]:12} {name}  [{kind}]{extra}")
only_here = sorted(set(target) - set(source)) if not ONLY else []
if only_here:
    print(f"only here, not in the file (left as they are): {', '.join(only_here)}")
if missing:
    print("PROBLEM: tables missing here (create them first; these objects would fail):")
    for t, objs in sorted(missing.items()):
        print(f"  {t}: read by {', '.join(sorted(objs))}")
if W["mode"] != "apply":
    dbutils.notebook.exit(f"dry run: {counts}; set mode = apply")

# COMMAND ----------

# MAGIC %md ## Import: apply

# COMMAND ----------

todo = [n for n in order if plan[n] != "same"]
failed: dict[str, str] = {}
for _attempt in (1, 2):  # a second pass for anything that failed on an object created later
    failed = {}
    for name in todo:
        o = source[name]
        try:
            if plan[name] == "kind changed":
                run_sql(engine, drop_other_kind(SCHEMA, name, o["type"]))
            run_sql(engine, create_or_alter(o["definition"], SRC, SCHEMA))
            print(f"{plan[name]:12} {name}: done")
        except Exception as e:  # report every object, then stop below
            failed[name] = str(e).splitlines()[0][:400]
    todo = list(failed)
    if not todo:
        break

after = modules(SCHEMA)
still = [n for n, o in source.items() if n not in after or not same(
    create_or_alter(o["definition"], SRC, SCHEMA), after[n]["definition"] or "")]  # fmt: skip
for name, err in failed.items():
    print(f"FAILED {name}: {err}")
print(f"done: {len(source) - len(still)} of {len(source)} objects equal to the file")
assert not still, f"not equal to the file: {still}"
