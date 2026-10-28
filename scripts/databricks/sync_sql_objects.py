# Databricks notebook source
# MAGIC %md
# MAGIC # Copy functions and procedures from one environment to another
# MAGIC
# MAGIC Copies every user-defined function (scalar, inline and multi-statement table functions) and
# MAGIC stored procedure of a schema, e.g. `dbo_professional360` from `dv` to `pp`, with the
# MAGIC definitions as they are in the source (`sys.sql_modules`), including those the BFF team
# MAGIC wrote there. Tables and data are not touched.
# MAGIC
# MAGIC - An object new or different in the source is created with `CREATE OR ALTER`, in dependency
# MAGIC   order (a function before the functions and procedures that call it). One whose kind
# MAGIC   differs in the target (e.g. inline vs multi-statement function, error 2010) is dropped
# MAGIC   there first. An object already the same is left alone, so a rerun changes nothing.
# MAGIC - Objects only in the target are listed, never dropped.
# MAGIC - Tables the copied objects read but the target lacks are listed by the dry run: create
# MAGIC   them first (e.g. `UserNotificationRead`, in `deploy/sql/bff_notifications.sql`).
# MAGIC - Permissions (`GRANT EXECUTE ...`) are not copied: principals differ between environments.
# MAGIC
# MAGIC Run with `mode = dry_run` first, read the list, then `mode = apply`. Connects with
# MAGIC `azure_sql_connect` (D-057); the two environments' settings come from `config/azure.yaml`.

# COMMAND ----------

# MAGIC %pip install pyodbc pyyaml

# COMMAND ----------

# MAGIC %run ./azure_sql_connect

# COMMAND ----------

dbutils.widgets.dropdown("source_env", "dv", ["dv", "pp", "pr"], "1. Source environment")
dbutils.widgets.dropdown("target_env", "pp", ["dv", "pp", "pr"], "2. Target environment")
dbutils.widgets.dropdown("mode", "dry_run", ["dry_run", "apply"], "3. Mode")
# Blank: app_schema of config/azure.yaml for each environment.
dbutils.widgets.text("source_schema", "", "4. Source schema (blank: azure.yaml)")
dbutils.widgets.text("target_schema", "", "5. Target schema (blank: azure.yaml)")
# Comma-separated object names to copy; blank: all of them.
dbutils.widgets.text("only", "", "6. Only these objects (blank: all)")

# COMMAND ----------

import re
from graphlib import CycleError, TopologicalSorter

W = {k: dbutils.widgets.get(k).strip() for k in (
    "source_env", "target_env", "mode", "source_schema", "target_schema", "only")}  # fmt: skip
SRC_S = azure_sql_settings(W["source_env"])
DST_S = azure_sql_settings(W["target_env"])
SRC = W["source_schema"] or SRC_S.get("app_schema") or "dbo_professional360"
DST = W["target_schema"] or DST_S.get("app_schema") or "dbo_professional360"
assert (SRC_S["server"], SRC_S["database"], SRC) != (DST_S["server"], DST_S["database"], DST), (
    "source and target are the same"
)
src = sql_engine(SRC_S)
dst = sql_engine(DST_S)
print(f"source {SRC_S['server']} / {SRC_S['database']} / {SRC} ({W['source_env']})")
print(f"target {DST_S['server']} / {DST_S['database']} / {DST} ({W['target_env']})")

KINDS = {"FN": "scalar function", "IF": "inline table function",
         "TF": "multi-statement table function", "P": "procedure"}  # fmt: skip


def modules(engine, schema: str) -> dict[str, dict]:
    """Functions and procedures of `schema`: name -> {type, definition, object_id}."""
    rows = query(engine, f"""
        SELECT o.object_id, o.name, RTRIM(o.type) AS type, m.definition
        FROM sys.objects o
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        LEFT JOIN sys.sql_modules m ON m.object_id = o.object_id
        WHERE s.name = N'{schema}' AND o.type IN ('FN', 'IF', 'TF', 'P')""")  # fmt: skip
    return {r["name"]: r for r in rows}


source = modules(src, SRC)
target = modules(dst, DST)
if W["only"]:
    wanted = {n.strip() for n in W["only"].split(",") if n.strip()}
    unknown = wanted - source.keys()
    assert not unknown, f"not in the source schema: {sorted(unknown)}"
    source = {n: r for n, r in source.items() if n in wanted}
hidden = sorted(n for n, r in source.items() if r["definition"] is None)
assert not hidden, f"definitions not readable (encrypted or no VIEW DEFINITION): {hidden}"
print(f"source: {len(source)} objects; target: {len(target)}")

# COMMAND ----------

# MAGIC %md ## Plan

# COMMAND ----------

LEAD = r"(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*"  # blanks and comments before CREATE
HEAD = re.compile(rf"^({LEAD})CREATE\s+(?:OR\s+ALTER\s+)?(FUNCTION|PROCEDURE|PROC)\b",
                  re.IGNORECASE | re.DOTALL)  # fmt: skip


def for_target(definition: str) -> str:
    """The source definition as CREATE OR ALTER, with the target's schema name."""
    m = HEAD.match(definition)
    assert m, f"no CREATE FUNCTION / PROCEDURE at the start of:\n{definition[:300]}"
    sql = f"{m.group(1)}CREATE OR ALTER {m.group(2)}{definition[m.end() :]}"
    if SRC.lower() != DST.lower():
        sql = re.sub(rf"(\[{re.escape(SRC)}\]|\b{re.escape(SRC)}\b)\.", f"[{DST}].", sql,
                     flags=re.IGNORECASE)  # fmt: skip
    return sql


def same(a: str, b: str) -> bool:
    """Equal apart from line endings, trailing blanks and CREATE vs CREATE OR ALTER."""

    def norm(s: str) -> str:
        s = HEAD.sub(lambda m: f"{m.group(1)}CREATE {m.group(2).upper()}", s, count=1)
        return "\n".join(ln.rstrip() for ln in s.replace("\r\n", "\n").strip().splitlines())

    return norm(a) == norm(b)


plan: dict[str, str] = {}  # name -> new | changed | kind changed | same
for name, r in source.items():
    t = target.get(name)
    if t is None:
        plan[name] = "new"
    elif t["type"] != r["type"] and not {t["type"], r["type"]} <= {"P"}:
        plan[name] = "kind changed"
    elif same(for_target(r["definition"]), t["definition"] or ""):
        plan[name] = "same"
    else:
        plan[name] = "changed"

# Dependency order within the copied objects (the source's own references).
ids = {r["object_id"]: n for n, r in source.items()}
deps = query(src, f"""
    SELECT d.referencing_id, d.referenced_id
    FROM sys.sql_expression_dependencies d
    JOIN sys.objects o ON o.object_id = d.referencing_id
    WHERE SCHEMA_NAME(o.schema_id) = N'{SRC}' AND d.referenced_id IS NOT NULL""")  # fmt: skip
graph = {n: set() for n in source}
for d in deps:
    a, b = ids.get(d["referencing_id"]), ids.get(d["referenced_id"])
    if a and b and a != b:
        graph[a].add(b)
try:
    order = list(TopologicalSorter(graph).static_order())
except CycleError:
    order = sorted(source, key=lambda n: (source[n]["type"] == "P", n))  # retried below anyway

# Tables (and views) the objects read that the target lacks.
reads = query(src, f"""
    SELECT DISTINCT o.name AS obj, ISNULL(d.referenced_schema_name, N'{SRC}') AS sch,
           d.referenced_entity_name AS tbl
    FROM sys.sql_expression_dependencies d
    JOIN sys.objects o ON o.object_id = d.referencing_id
    LEFT JOIN sys.objects r ON r.object_id = d.referenced_id
    WHERE SCHEMA_NAME(o.schema_id) = N'{SRC}' AND d.referenced_database_name IS NULL
      AND (r.type IN ('U', 'V') OR r.object_id IS NULL)""")  # fmt: skip
tables = query(dst, "SELECT SCHEMA_NAME(schema_id) AS sch, name FROM sys.objects "
                    "WHERE type IN ('U', 'V')")  # fmt: skip
have = {(r["sch"].lower(), r["name"].lower()) for r in tables}
missing: dict[str, set[str]] = {}
for r in reads:
    if r["obj"] not in source or r["tbl"] in source:
        continue
    sch = DST if r["sch"].lower() == SRC.lower() else r["sch"]
    if (sch.lower(), r["tbl"].lower()) not in have:
        missing.setdefault(f"{sch}.{r['tbl']}", set()).add(r["obj"])

counts = {k: sum(1 for v in plan.values() if v == k)
          for k in ("new", "changed", "kind changed", "same")}  # fmt: skip
print("plan:", counts)
for name in order:
    if plan[name] != "same":
        kind = KINDS[source[name]["type"]]
        extra = f" (target: {KINDS[target[name]['type']]})" if plan[name] == "kind changed" else ""
        print(f"  {plan[name]:12} {name}  [{kind}]{extra}")
only_target = sorted(set(target) - set(source)) if not W["only"] else []
if only_target:
    print(f"only in the target (left as they are): {', '.join(only_target)}")
if missing:
    print("PROBLEM: tables missing in the target (create them first; these objects would fail):")
    for t, objs in sorted(missing.items()):
        print(f"  {t}: read by {', '.join(sorted(objs))}")
if W["mode"] != "apply":
    dbutils.notebook.exit(f"dry run: {counts}; set mode = apply")

# COMMAND ----------

# MAGIC %md ## Apply

# COMMAND ----------

todo = [n for n in order if plan[n] != "same"]
failed: dict[str, str] = {}
for _attempt in (1, 2):  # a second pass for anything that failed on an object created later
    failed = {}
    for name in todo:
        try:
            if plan[name] == "kind changed" and name in target:
                kw = "PROCEDURE" if target[name]["type"] == "P" else "FUNCTION"
                run_sql(dst, f"DROP {kw} IF EXISTS [{DST}].[{name}]")
                target.pop(name)
            run_sql(dst, for_target(source[name]["definition"]))
            print(f"{plan[name]:12} {name}: done")
        except Exception as e:  # report every object, then stop below
            failed[name] = str(e).splitlines()[0][:400]
    todo = list(failed)
    if not todo:
        break

after = modules(dst, DST)
still = [n for n in source if n not in after or not same(
    for_target(source[n]["definition"]), after[n]["definition"] or "")]  # fmt: skip
for name, err in failed.items():
    print(f"FAILED {name}: {err}")
print(f"done: {len(source) - len(still)} of {len(source)} objects equal to the source")
assert not still, f"not equal to the source: {still}"
