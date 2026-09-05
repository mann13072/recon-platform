from packages.observability.alerts import ALERT_RULES
from scripts.generate_monitoring_config import render


def test_declared_alert_thresholds_drive_evaluation() -> None:
    for rule in ALERT_RULES:
        assert not rule.predicate(rule.threshold)
        outside = rule.threshold + 0.01 if rule.operator == "gt" else rule.threshold - 0.01
        assert rule.predicate(outside)


def test_prometheus_rules_are_generated_from_every_declaration() -> None:
    generated = render()
    assert generated.count("      - alert:") == len(ALERT_RULES) == 8
    for rule in ALERT_RULES:
        symbol = ">" if rule.operator == "gt" else "<"
        rendered = str(int(rule.threshold)) if rule.threshold.is_integer() else str(rule.threshold)
        assert f"expr: {rule.metric} {symbol} {rendered}" in generated
