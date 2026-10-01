"""Generate the dashboard's SLO row from observability/slos.yaml (Phase 7).

Why it exists: the SLO targets live in one file. This script rewrites the
"SLOs" row of `observability/dashboards/hospital-ops.json` from it, so a
target can't drift between the config and the panels. Re-run it after
editing `slos.yaml`; Grafana picks the JSON up within 30 s (provisioning
rescan).

Per SLO it builds three panels:
- the SLI over the dashboard's range, green at or above target;
- the error budget remaining, 1 - (1 - SLI) / (1 - target): 100% means no
  bad events, 0% means the budget is spent, negative means the SLO is
  missed;
- the SLI over time, as a 1 h rolling window with the target as a line.

Run from the repo root:
    backend/.venv/Scripts/python.exe observability/scripts/build_slo_panels.py
"""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SLO_FILE = ROOT / "slos.yaml"
DASHBOARD = ROOT / "dashboards" / "hospital-ops.json"
ROW_TITLE_PREFIX = "SLOs"
DATASOURCE = {"type": "prometheus", "uid": "prometheus"}
FIRST_ID = 100  # clear of the hand-numbered panels above


def _target(expr: str, legend: str, *, instant: bool) -> dict:
    return {
        "refId": "A",
        "datasource": DATASOURCE,
        "expr": expr,
        "legendFormat": legend,
        "range": not instant,
        "instant": instant,
        "exemplar": False,
    }


def _stat(panel_id: int, title: str, description: str, expr: str, x: int, y: int, steps: list) -> dict:
    return {
        "type": "stat",
        "id": panel_id,
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": 4, "h": 5},
        "targets": [_target(expr, "", instant=True)],
        "fieldConfig": {
            "defaults": {
                "unit": "percentunit",
                "decimals": 2,
                "noValue": "no traffic in range",
                "thresholds": {"mode": "absolute", "steps": steps},
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "background",
            "graphMode": "none",
            "textMode": "value",
            "justifyMode": "center",
        },
    }


def build_panels(slos: list[dict], top: int) -> list[dict]:
    panels: list[dict] = [
        {
            "type": "row",
            "id": FIRST_ID,
            "title": f"{ROW_TITLE_PREFIX}: targets from observability/slos.yaml (green = met)",
            "collapsed": False,
            "gridPos": {"x": 0, "y": top, "w": 24, "h": 1},
            "panels": [],
        }
    ]
    next_id = FIRST_ID + 1
    stat_y = top + 1
    series_y = stat_y + 5
    for index, slo in enumerate(slos):
        target = float(slo["target"])
        sli = f"({slo['good']}) / ({slo['total']})"
        budget = f"1 - (1 - {sli}) / (1 - {target})"
        x = index * 8
        panels.append(
            _stat(
                next_id,
                f"{slo['name']}: SLI (target {target:.1%})",
                f"{slo['sli']}. {slo['why']}",
                sli,
                x,
                stat_y,
                [{"color": "red", "value": None}, {"color": "green", "value": target}],
            )
        )
        panels.append(
            _stat(
                next_id + 1,
                f"{slo['name']}: error budget left",
                "1 - (1 - SLI) / (1 - target) over the dashboard's range. 100% = no "
                "bad events, 0% = budget spent, below 0 = SLO missed.",
                budget,
                x + 4,
                stat_y,
                [
                    {"color": "red", "value": None},
                    {"color": "orange", "value": 0},
                    {"color": "green", "value": 0.25},
                ],
            )
        )
        rolling = sli.replace("[$__range]", "[1h]")
        panels.append(
            {
                "type": "timeseries",
                "id": next_id + 2,
                "title": f"{slo['name']}: SLI, 1 h rolling",
                "description": slo["question"],
                "datasource": DATASOURCE,
                "gridPos": {"x": x, "y": series_y, "w": 8, "h": 7},
                "targets": [_target(rolling, "SLI", instant=False)],
                "fieldConfig": {
                    "defaults": {
                        "unit": "percentunit",
                        "max": 1,
                        "custom": {
                            "lineWidth": 2,
                            "showPoints": "never",
                            "thresholdsStyle": {"mode": "dashed"},
                        },
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [
                                {"color": "red", "value": None},
                                {"color": "green", "value": target},
                            ],
                        },
                        "color": {"mode": "fixed", "fixedColor": "blue"},
                    },
                    "overrides": [],
                },
                "options": {
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": False},
                    "tooltip": {"mode": "single"},
                },
            }
        )
        next_id += 3
    return panels


def main() -> None:
    slos = yaml.safe_load(SLO_FILE.read_text(encoding="utf-8"))["slos"]
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    kept: list[dict] = []
    for panel in dashboard["panels"]:
        # Drop the previous SLO row (reserved placeholder or a prior build).
        is_slo_row = panel["type"] == "row" and panel.get("title", "").startswith(ROW_TITLE_PREFIX)
        if is_slo_row or panel["id"] >= FIRST_ID:
            continue
        kept.append(panel)
    top = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in kept)
    dashboard["panels"] = kept + build_panels(slos, top)
    DASHBOARD.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(slos)} SLOs ({len(slos) * 3} panels) into {DASHBOARD}")


if __name__ == "__main__":
    main()
