"""Choix d'un réglage déjà mesuré. Un écart en régression n'est pas gardé."""

METRIC_KEYS = ("iops", "p99_us", "throughput_bps", "cpu_per_gb")


def _loads(result):
    loads = (result or {}).get("workloads") or {}
    if not isinstance(loads, dict) or not loads:
        return None
    return loads


def complete(result):
    loads = _loads(result)
    if loads is None:
        return False
    for item in loads.values():
        if not isinstance(item, dict):
            return False
        for key in METRIC_KEYS:
            if not isinstance(item.get(key), (int, float)):
                return False
    return True


def regressed(baseline, trial):
    """Vrai si une charge perd plus de 5 % en p99, IOPS ou débit."""
    base = _loads(baseline)
    got = _loads(trial)
    if base is None or got is None or set(base) != set(got):
        return True
    for name, before in base.items():
        after = got[name]
        if after["p99_us"] > before["p99_us"] * 1.05:
            return True
        if after["iops"] < before["iops"] * 0.95:
            return True
        if after["throughput_bps"] < before["throughput_bps"] * 0.95:
            return True
    return False


def improved(baseline, trial):
    base = _loads(baseline)
    got = _loads(trial)
    if base is None or got is None:
        return False
    for name, before in base.items():
        after = got[name]
        if after["p99_us"] < before["p99_us"] * 0.98:
            return True
        if after["iops"] > before["iops"] * 1.02:
            return True
    return False


def summary_p99(result):
    loads = _loads(result) or {}
    if not loads:
        return None
    return max(item["p99_us"] for item in loads.values())


def select_keep(baseline, trials):
    """Au plus un réglage, le meilleur p99, et seulement s'il bat la base."""
    if not complete(baseline):
        return []
    best = None
    for name, trial in (trials or {}).items():
        if name == "baseline":
            continue
        change = (trial or {}).get("change")
        if not isinstance(change, dict) or len(change) != 1:
            continue
        if not complete(trial) or regressed(baseline, trial) or not improved(baseline, trial):
            continue
        p99 = summary_p99(trial)
        if best is None or p99 < best["p99_us"]:
            best = {"name": name, "change": change, "p99_us": p99}
    if best is None:
        return []
    return [best]


def assert_kept_safe(baseline, trials, kept):
    """Lève si un réglage gardé n'a pas son banc, ou s'il régresse."""
    if len(kept) > 1:
        raise ValueError("plus d'un réglage gardé")
    for item in kept:
        trial = (trials or {}).get(item["name"])
        if trial is None or regressed(baseline, trial):
            raise ValueError("régression gardée : {}".format(item["name"]))
        if item["change"] != trial.get("change"):
            raise ValueError("réglage gardé différent du banc")


def render_table(document):
    lines = [
        "réglage\tcharge\tiops\tp99_us\tdébit_o/s\tcpu_par_go\tgardé",
    ]
    kept_name = None
    for item in document.get("keep") or []:
        kept_name = item.get("name")
    trials = document.get("trials") or {}
    for name in sorted(trials):
        trial = trials[name]
        flag = "oui" if name == kept_name else "non"
        for load, metric in sorted((trial.get("workloads") or {}).items()):
            lines.append("{}\t{}\t{}\t{}\t{}\t{}\t{}".format(
                name,
                load,
                metric.get("iops"),
                metric.get("p99_us"),
                metric.get("throughput_bps"),
                metric.get("cpu_per_gb"),
                flag,
            ))
    if not trials:
        lines.append("aucun banc\t-\t-\t-\t-\t-\tnon")
    return "\n".join(lines) + "\n"
