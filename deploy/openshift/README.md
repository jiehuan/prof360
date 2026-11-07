# OpenShift deployment (Phase 6, decisions D-034)

Placeholders to replace (`grep -rn PLACEHOLDER deploy/`):
- `PLACEHOLDER_NAMESPACE`: namespace / project (follow the existing `professional-cronjob`)
- `PLACEHOLDER_IMAGE`: Artifactory image path; Harness sets the tag
- labels and secret names, to match the team's conventions

Alpha Vantage defaults to `ALPHAVANTAGE_MODE=direct`. To use APIM, set the mode to `apim`,
inject the API key plus the three `ALPHAVANTAGE_CLIENT_ID/CLIENT_SECRET/SUBSCRIPTION_KEY`
credentials listed in `secret.example.yaml`, and configure/mount `config/alphavantage.yaml`.
`ALPHAVANTAGE_ENV` selects its environment profile. Mode/environment are non-secret and may
live in your ConfigMap; credential values belong in your Secret or private Helm values.
See [Alpha Vantage APIM setup](../../docs/ALPHAVANTAGE_APIM.zh-CN.md).

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
- Logs (D-089): stdout is one JSON object per line, with `run_id`, `ticker` and `stage`. It is
  concise: one line per stage, warnings and errors, and the run summary; per-company detail is
  in `pipeline_task.detail` (`uw-pipeline runs`) and comes back with `LOG_LEVEL=DEBUG`. Each run
  also writes a text file to `/data/logs` on the PVC (`LOG_DIR`), kept 30 days
  (`LOG_KEEP_DAYS`).
- Reading the logs without `oc`: in the web console, open the Terminal of a running
  uw-nightly / uw-morning pod, or create the temporary pod in `log-viewer.yaml` (Jobs -> Create
  Job) and run `uw-pipeline logs` in its Terminal. Pod logs (the Logs tab) of the last three
  jobs stay for 7 days.

Dev uses SQLite on the PVC (`DATABASE_URL=sqlite:////data/uw.db`). SQLite allows one writer at
a time, so jobs must not overlap: nightly (23:30) and morning (05:30) are 6 hours apart, and
`news` runs at 03:00, 09:00, 15:00 and 21:00 ET with the premium Alpha Vantage key. The
`publish` stage writes the application database (`APP_DATABASE_URL`, dev default
`sqlite:////data/app.db`). Before Azure SQL: set `DATABASE_URL` in the
secret, decide who owns the schema migrations, and test the MERGE upsert path. It has not run
against SQL Server yet.
