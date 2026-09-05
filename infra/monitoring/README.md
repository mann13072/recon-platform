# Monitoring configuration

`alerts.yml` is generated from `packages/observability/alerts.py`, the single source of
truth for names, metrics, operators, thresholds, severity, response actions, and runbooks.
Regenerate it after changing a rule:

```shell
python scripts/generate_monitoring_config.py
```

Prometheus loads the generated rules and scrapes the API and worker. Import
`grafana-dashboard.json` with a Prometheus datasource variable named `DS_PROMETHEUS`.
