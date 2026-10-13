# OpenShift deployment (Phase 6, decisions D-034)

Placeholders to replace (`grep -rn PLACEHOLDER deploy/`):
- `PLACEHOLDER_NAMESPACE`: namespace / project (follow the existing `professional-cronjob`)
- `PLACEHOLDER_IMAGE`: Artifactory image path; Harness sets the tag
- labels and secret names, to match the team's conventions

Apply order:
```
oc apply -f pvc.yaml
oc create secret generic uw-pipeline-secrets --from-env-file=<file>   # keys: secret.example.yaml
oc apply -f cronjobs.yaml
```

Operations:
- `oc create job --from=cronjob/uw-nightly uw-nightly-manual-$(date +%s)` runs the nightly job now.
- `uw-pipeline runs` (in a pod) shows recent runs and failed tasks.
- A killed or failed pod is retried by the Job (`backoffLimit`). The retry resumes the same run
  and continues with unfinished tasks (`pipeline_run` / `pipeline_task`).
- Scheduled jobs re-fetch SEC metadata on every run (`refresh_sec` in config/jobs.yaml), so the
  05:30 catch-up sees filings made after the 23:30 run.
- Logs are one JSON object per line, with `run_id`, `ticker` and `stage`.

Dev uses SQLite on the PVC (`DATABASE_URL=sqlite:////data/uw.db`). SQLite allows one writer at
a time, so jobs must not overlap: nightly and morning are 6 hours apart, and `news` stays
suspended until a premium Alpha Vantage key exists. Before Azure SQL: set `DATABASE_URL` in the
secret, decide who owns the schema migrations, and test the MERGE upsert path. It has not run
against SQL Server yet.
