"""Sanitized operational alert evaluation for the hosted finance service."""


def evaluate_storage_alerts(*, journal_integrity, pool_stats, metrics,
                            journal_audit_fresh=True):
    alerts = []
    if not journal_integrity:
        alerts.append({"code": "JOURNAL_INTEGRITY_FAILED", "severity": "CRITICAL"})
    if not journal_audit_fresh:
        alerts.append({"code": "JOURNAL_DEEP_AUDIT_STALE", "severity": "CRITICAL"})
    counters = metrics.get("counters", {})
    if counters.get("pool_timeouts", 0) > 0:
        alerts.append({"code": "DATABASE_POOL_TIMEOUT_OBSERVED", "severity": "WARNING"})
    for role in ("api", "worker"):
        values = pool_stats.get(role)
        if not isinstance(values, dict):
            continue
        if values.get("requests_waiting", 0) > 0:
            alerts.append({"code": "DATABASE_POOL_WAITERS_" + role.upper(),
                           "severity": "WARNING"})
        maximum = values.get("pool_max")
        available = values.get("pool_available")
        size = values.get("pool_size")
        if (isinstance(maximum, int) and isinstance(size, int)
                and isinstance(available, int) and size >= maximum and available == 0):
            alerts.append({"code": "DATABASE_POOL_SATURATED_" + role.upper(),
                           "severity": "WARNING"})
    return sorted(alerts, key=lambda item: (item["severity"], item["code"]))
