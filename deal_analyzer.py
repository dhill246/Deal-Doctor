"""
Deal Doctor — HubSpot Pipeline Diagnosis Tool
Connects to any HubSpot portal, pulls deal data, and generates interactive reports.

Usage:
    py deal_analyzer.py schema                         # Fetch deal schema (properties, pipelines, owners)
    py deal_analyzer.py analyze --config config.json   # Run analysis and generate PDF report
"""

import argparse
import json
import math
import os
import sys
import tempfile
import urllib.request
import urllib.parse
import urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, stdev

from dotenv import load_dotenv

# Load .env from script directory
load_dotenv(Path(__file__).parent / ".env")

BASE_URL = "https://api.hubapi.com"
TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")

SCRIPT_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# HubSpot API helpers
# ---------------------------------------------------------------------------

def api_request(method, path, data=None, params=None):
    """Make an authenticated request to the HubSpot API."""
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }

    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        print(f"API Error {e.code}: {error_body}", file=sys.stderr)
        raise


def paginate_search(path, body, limit=100):
    """Paginate through HubSpot search API results."""
    results = []
    after = 0
    body = dict(body)
    body["limit"] = limit

    while True:
        body["after"] = after
        resp = api_request("POST", path, data=body)
        batch = resp.get("results", [])
        results.extend(batch)
        paging = resp.get("paging", {})
        next_page = paging.get("next", {})
        if next_page.get("after"):
            after = int(next_page["after"])
        else:
            break

    return results


def paginate_list(path, params=None):
    """Paginate through HubSpot list/GET API results."""
    results = []
    params = dict(params or {})

    while True:
        resp = api_request("GET", path, params=params)
        results.extend(resp.get("results", []))
        paging = resp.get("paging", {})
        next_page = paging.get("next", {})
        if next_page.get("after"):
            params["after"] = next_page["after"]
        else:
            break

    return results


# ---------------------------------------------------------------------------
# Schema command
# ---------------------------------------------------------------------------

def cmd_schema(_args):
    """Fetch and output deal schema as JSON."""
    if not TOKEN:
        print("ERROR: HUBSPOT_ACCESS_TOKEN not found in .env", file=sys.stderr)
        sys.exit(1)

    print("Fetching deal properties...", file=sys.stderr)
    props_raw = api_request("GET", "/crm/v3/properties/deals")
    properties = []
    for p in props_raw.get("results", []):
        properties.append({
            "name": p["name"],
            "label": p["label"],
            "type": p["type"],
            "fieldType": p.get("fieldType", ""),
            "groupName": p.get("groupName", ""),
            "options": [{"label": o["label"], "value": o["value"]}
                        for o in p.get("options", [])],
        })

    print("Fetching deal pipelines...", file=sys.stderr)
    pipelines_raw = api_request("GET", "/crm/v3/pipelines/deals")
    pipelines = []
    for pl in pipelines_raw.get("results", []):
        stages = []
        for s in pl.get("stages", []):
            stages.append({
                "id": s["id"],
                "label": s["label"],
                "displayOrder": s.get("displayOrder", 0),
                "metadata": s.get("metadata", {}),
            })
        stages.sort(key=lambda x: x["displayOrder"])
        pipelines.append({
            "id": pl["id"],
            "label": pl["label"],
            "stages": stages,
        })

    print("Fetching owners...", file=sys.stderr)
    owners_raw = paginate_list("/crm/v3/owners/")
    owners = []
    for o in owners_raw:
        owners.append({
            "id": o["id"],
            "name": f"{o.get('firstName', '')} {o.get('lastName', '')}".strip(),
            "email": o.get("email", ""),
        })

    schema = {
        "properties": properties,
        "pipelines": pipelines,
        "owners": owners,
    }
    print(json.dumps(schema, indent=2))


# ---------------------------------------------------------------------------
# Deal fetching
# ---------------------------------------------------------------------------

def fetch_deals(config, pipeline_stages):
    """Fetch all closed won + closed lost deals matching the config."""
    closed_stages = config["closed_won_stages"] + config["closed_lost_stages"]

    base_props = [
        "dealname", "amount", "closedate", "createdate",
        "dealstage", "pipeline", "hubspot_owner_id",
        "hs_analytics_source",
    ]
    extra_props = config.get("properties", [])
    all_props = list(set(base_props + extra_props))

    filters = [
        {
            "propertyName": "dealstage",
            "operator": "IN",
            "values": closed_stages,
        }
    ]

    time_period = config.get("time_period")
    if time_period:
        if time_period.get("start"):
            filters.append({
                "propertyName": "closedate",
                "operator": "GTE",
                "value": _to_epoch_ms(time_period["start"]),
            })
        if time_period.get("end"):
            filters.append({
                "propertyName": "closedate",
                "operator": "LTE",
                "value": _to_epoch_ms(time_period["end"]),
            })

    pipeline_id = config.get("pipeline_id")
    if pipeline_id and pipeline_id != "all":
        filters.append({
            "propertyName": "pipeline",
            "operator": "EQ",
            "value": pipeline_id,
        })

    body = {
        "filterGroups": [{"filters": filters}],
        "properties": all_props,
        "sorts": [{"propertyName": "closedate", "direction": "DESCENDING"}],
    }

    print("Fetching deals...", file=sys.stderr)
    deals = paginate_search("/crm/v3/objects/deals/search", body)
    print(f"  Retrieved {len(deals)} deals", file=sys.stderr)

    if len(deals) >= 10000:
        print("  Search API limit reached, falling back to list API...", file=sys.stderr)
        params = {"properties": ",".join(all_props), "limit": "100"}
        all_deals = paginate_list("/crm/v3/objects/deals", params)
        deals = [
            d for d in all_deals
            if d.get("properties", {}).get("dealstage") in closed_stages
        ]
        if time_period:
            deals = _filter_by_time(deals, time_period)
        if pipeline_id and pipeline_id != "all":
            deals = [d for d in deals if d.get("properties", {}).get("pipeline") == pipeline_id]
        print(f"  After filtering: {len(deals)} deals", file=sys.stderr)

    return deals


def _to_epoch_ms(date_str):
    """Convert YYYY-MM-DD to epoch milliseconds."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp() * 1000))


def _filter_by_time(deals, time_period):
    """Client-side time filter for deals."""
    filtered = []
    start = time_period.get("start")
    end = time_period.get("end")
    for d in deals:
        cd = d.get("properties", {}).get("closedate")
        if not cd:
            continue
        close_date = cd[:10]
        if start and close_date < start:
            continue
        if end and close_date > end:
            continue
        filtered.append(d)
    return filtered


# ---------------------------------------------------------------------------
# Metrics engine (enhanced)
# ---------------------------------------------------------------------------

def compute_metrics(deals, config, pipeline_stages, owners_map):
    """Compute all analysis metrics from deal data."""
    won_stages = set(config["closed_won_stages"])
    lost_stages = set(config["closed_lost_stages"])
    focus = set(config.get("focus_areas", []))

    parsed = []
    for d in deals:
        props = d.get("properties", {})
        stage = props.get("dealstage", "")
        amount = _safe_float(props.get("amount"))
        create_date = _parse_date(props.get("createdate"))
        close_date = _parse_date(props.get("closedate"))
        cycle_days = None
        if create_date and close_date:
            cycle_days = (close_date - create_date).days

        parsed.append({
            "id": d.get("id"),
            "name": props.get("dealname", "Unknown"),
            "amount": amount,
            "stage": stage,
            "is_won": stage in won_stages,
            "is_lost": stage in lost_stages,
            "pipeline": props.get("pipeline", ""),
            "owner_id": props.get("hubspot_owner_id", ""),
            "source": props.get("hs_analytics_source", "Unknown"),
            "create_date": create_date,
            "close_date": close_date,
            "cycle_days": cycle_days,
            "close_month": close_date.strftime("%Y-%m") if close_date else None,
            "close_quarter": f"{close_date.year} Q{(close_date.month - 1) // 3 + 1}" if close_date else None,
            "raw_props": props,
        })

    won = [d for d in parsed if d["is_won"]]
    lost = [d for d in parsed if d["is_lost"]]

    metrics = {}

    # --- Executive Summary ---
    won_amounts = [d["amount"] for d in won if d["amount"] is not None]
    lost_amounts = [d["amount"] for d in lost if d["amount"] is not None]
    won_cycles = [d["cycle_days"] for d in won if d["cycle_days"] is not None and d["cycle_days"] >= 0]
    lost_cycles = [d["cycle_days"] for d in lost if d["cycle_days"] is not None and d["cycle_days"] >= 0]

    metrics["executive_summary"] = {
        "total_deals": len(parsed),
        "won_count": len(won),
        "lost_count": len(lost),
        "win_rate": len(won) / len(parsed) * 100 if parsed else 0,
        "total_won_value": sum(won_amounts),
        "total_lost_value": sum(lost_amounts),
        "avg_won_deal_size": mean(won_amounts) if won_amounts else 0,
        "avg_lost_deal_size": mean(lost_amounts) if lost_amounts else 0,
        "median_won_deal_size": median(won_amounts) if won_amounts else 0,
        "avg_won_cycle_days": mean(won_cycles) if won_cycles else 0,
        "avg_lost_cycle_days": mean(lost_cycles) if lost_cycles else 0,
    }

    # --- Win/Loss Breakdown ---
    metrics["win_loss"] = {
        "won": {"count": len(won), "total_value": sum(won_amounts), "avg_value": mean(won_amounts) if won_amounts else 0},
        "lost": {"count": len(lost), "total_value": sum(lost_amounts), "avg_value": mean(lost_amounts) if lost_amounts else 0},
    }

    # --- Deal Size Distribution ---
    all_amounts = [d["amount"] for d in parsed if d["amount"] is not None and d["amount"] > 0]
    if all_amounts:
        sorted_amounts = sorted(all_amounts)
        q1_idx = len(sorted_amounts) // 4
        q3_idx = 3 * len(sorted_amounts) // 4
        metrics["deal_size_distribution"] = {
            "count": len(all_amounts),
            "min": min(all_amounts),
            "max": max(all_amounts),
            "mean": mean(all_amounts),
            "median": median(all_amounts),
            "q1": sorted_amounts[q1_idx],
            "q3": sorted_amounts[q3_idx],
            "stdev": stdev(all_amounts) if len(all_amounts) > 1 else 0,
            "values": all_amounts,
            "won_values": [d["amount"] for d in won if d["amount"] is not None and d["amount"] > 0],
            "lost_values": [d["amount"] for d in lost if d["amount"] is not None and d["amount"] > 0],
        }
    else:
        metrics["deal_size_distribution"] = None

    # --- Sales Cycle Analysis ---
    metrics["sales_cycle"] = {
        "won_cycles": won_cycles,
        "lost_cycles": lost_cycles,
        "avg_won": mean(won_cycles) if won_cycles else 0,
        "avg_lost": mean(lost_cycles) if lost_cycles else 0,
        "median_won": median(won_cycles) if won_cycles else 0,
        "median_lost": median(lost_cycles) if lost_cycles else 0,
    }

    # --- Deal Velocity (amount / cycle time) ---
    velocity_won = []
    velocity_lost = []
    for d in parsed:
        if d["amount"] and d["cycle_days"] and d["cycle_days"] > 0:
            vel = d["amount"] / d["cycle_days"]
            entry = {"name": d["name"], "amount": d["amount"], "cycle_days": d["cycle_days"], "velocity": vel,
                     "owner_id": d["owner_id"], "source": d["source"]}
            if d["is_won"]:
                velocity_won.append(entry)
            elif d["is_lost"]:
                velocity_lost.append(entry)
    metrics["deal_velocity"] = {
        "won": sorted(velocity_won, key=lambda x: x["velocity"], reverse=True),
        "lost": sorted(velocity_lost, key=lambda x: x["velocity"], reverse=True),
        "avg_won_velocity": mean([v["velocity"] for v in velocity_won]) if velocity_won else 0,
        "avg_lost_velocity": mean([v["velocity"] for v in velocity_lost]) if velocity_lost else 0,
    }

    # --- Win Rate by Deal Size Tier ---
    tier_deals = [d for d in parsed if d["amount"] is not None and d["amount"] > 0]
    if tier_deals:
        amounts_sorted = sorted([d["amount"] for d in tier_deals])
        p33 = amounts_sorted[len(amounts_sorted) // 3]
        p66 = amounts_sorted[2 * len(amounts_sorted) // 3]

        tiers = {"Small": [], "Mid-Market": [], "Large": []}
        for d in tier_deals:
            if d["amount"] <= p33:
                tiers["Small"].append(d)
            elif d["amount"] <= p66:
                tiers["Mid-Market"].append(d)
            else:
                tiers["Large"].append(d)

        tier_metrics = {}
        for tier_name, tier_list in tiers.items():
            t_won = [d for d in tier_list if d["is_won"]]
            t_amounts = [d["amount"] for d in tier_list if d["amount"]]
            t_won_amounts = [d["amount"] for d in t_won if d["amount"]]
            t_cycles = [d["cycle_days"] for d in t_won if d["cycle_days"] is not None and d["cycle_days"] >= 0]
            tier_metrics[tier_name] = {
                "total": len(tier_list),
                "won": len(t_won),
                "lost": len(tier_list) - len(t_won),
                "win_rate": len(t_won) / len(tier_list) * 100 if tier_list else 0,
                "avg_deal_size": mean(t_amounts) if t_amounts else 0,
                "total_won_value": sum(t_won_amounts),
                "avg_cycle_days": mean(t_cycles) if t_cycles else 0,
                "range_low": min(t_amounts) if t_amounts else 0,
                "range_high": max(t_amounts) if t_amounts else 0,
            }
        metrics["win_rate_by_tier"] = {
            "thresholds": {"small_max": p33, "mid_max": p66},
            "tiers": tier_metrics,
        }
    else:
        metrics["win_rate_by_tier"] = None

    # --- Revenue Concentration (Pareto) ---
    if won_amounts:
        # Sort won deals by amount descending to get names alongside values
        won_sorted = sorted([d for d in won if d["amount"] is not None and d["amount"] > 0],
                            key=lambda d: d["amount"], reverse=True)
        total_rev = sum(d["amount"] for d in won_sorted)
        cumulative = 0
        pareto_data = []
        for i, d in enumerate(won_sorted, 1):
            cumulative += d["amount"]
            pareto_data.append({
                "deal_rank": i,
                "deal_name": d["name"],
                "amount": d["amount"],
                "cumulative": cumulative,
                "cumulative_pct": cumulative / total_rev * 100,
                "deal_pct": i / len(won_sorted) * 100,
            })

        # Find how many deals make up 80% of revenue
        deals_for_80 = next((p["deal_rank"] for p in pareto_data if p["cumulative_pct"] >= 80), len(won_sorted))
        pct_deals_for_80 = deals_for_80 / len(won_sorted) * 100

        metrics["revenue_concentration"] = {
            "pareto_data": pareto_data,
            "deals_for_80_pct": deals_for_80,
            "pct_deals_for_80": pct_deals_for_80,
            "top_5_deals": pareto_data[:5],
            "total_revenue": total_rev,
            "gini_coefficient": _gini([d["amount"] for d in won_sorted]),
        }
    else:
        metrics["revenue_concentration"] = None

    # --- Time Trends ---
    months = sorted(set(d["close_month"] for d in parsed if d["close_month"]))
    time_trends = []
    for m in months:
        m_deals = [d for d in parsed if d["close_month"] == m]
        m_won = [d for d in m_deals if d["is_won"]]
        m_amounts = [d["amount"] for d in m_won if d["amount"] is not None]
        m_cycles = [d["cycle_days"] for d in m_won if d["cycle_days"] is not None and d["cycle_days"] >= 0]
        time_trends.append({
            "month": m,
            "total": len(m_deals),
            "won": len(m_won),
            "lost": len(m_deals) - len(m_won),
            "win_rate": len(m_won) / len(m_deals) * 100 if m_deals else 0,
            "total_won_value": sum(m_amounts),
            "avg_won_value": mean(m_amounts) if m_amounts else 0,
            "avg_cycle_days": mean(m_cycles) if m_cycles else 0,
        })
    metrics["time_trends"] = time_trends

    # --- Quarterly Trends ---
    quarters = sorted(set(d["close_quarter"] for d in parsed if d["close_quarter"]))
    quarterly = []
    for q in quarters:
        q_deals = [d for d in parsed if d["close_quarter"] == q]
        q_won = [d for d in q_deals if d["is_won"]]
        q_amounts = [d["amount"] for d in q_won if d["amount"] is not None]
        quarterly.append({
            "quarter": q,
            "total": len(q_deals),
            "won": len(q_won),
            "lost": len(q_deals) - len(q_won),
            "win_rate": len(q_won) / len(q_deals) * 100 if q_deals else 0,
            "total_won_value": sum(q_amounts),
            "avg_won_value": mean(q_amounts) if q_amounts else 0,
        })
    metrics["quarterly_trends"] = quarterly

    # --- Cycle Time vs Deal Size scatter data ---
    scatter_data = []
    for d in parsed:
        if d["amount"] and d["amount"] > 0 and d["cycle_days"] is not None and d["cycle_days"] >= 0:
            scatter_data.append({
                "amount": d["amount"],
                "cycle_days": d["cycle_days"],
                "is_won": d["is_won"],
            })
    metrics["cycle_vs_size"] = scatter_data

    # --- Rep Performance ---
    if "rep_performance" in focus:
        by_owner = defaultdict(list)
        for d in parsed:
            oid = d["owner_id"]
            by_owner[oid].append(d)
        rep_metrics = []
        for oid, od in by_owner.items():
            o_won = [d for d in od if d["is_won"]]
            o_amounts = [d["amount"] for d in o_won if d["amount"] is not None]
            o_cycles = [d["cycle_days"] for d in o_won if d["cycle_days"] is not None and d["cycle_days"] >= 0]
            o_velocities = []
            for d in o_won:
                if d["amount"] and d["cycle_days"] and d["cycle_days"] > 0:
                    o_velocities.append(d["amount"] / d["cycle_days"])

            rep_metrics.append({
                "owner_id": oid,
                "owner_name": owners_map.get(oid, oid or "Unassigned"),
                "total_deals": len(od),
                "won": len(o_won),
                "lost": len(od) - len(o_won),
                "win_rate": len(o_won) / len(od) * 100 if od else 0,
                "total_won_value": sum(o_amounts),
                "avg_won_value": mean(o_amounts) if o_amounts else 0,
                "avg_cycle_days": mean(o_cycles) if o_cycles else 0,
                "avg_velocity": mean(o_velocities) if o_velocities else 0,
            })
        rep_metrics.sort(key=lambda x: x["total_won_value"], reverse=True)
        metrics["rep_performance"] = rep_metrics

    # --- Source Analysis ---
    if "source_analysis" in focus:
        by_source = defaultdict(list)
        for d in parsed:
            by_source[d["source"] or "Unknown"].append(d)
        source_metrics = []
        for src, sd in by_source.items():
            s_won = [d for d in sd if d["is_won"]]
            s_amounts = [d["amount"] for d in s_won if d["amount"] is not None]
            s_cycles = [d["cycle_days"] for d in s_won if d["cycle_days"] is not None and d["cycle_days"] >= 0]
            source_metrics.append({
                "source": src,
                "total_deals": len(sd),
                "won": len(s_won),
                "lost": len(sd) - len(s_won),
                "win_rate": len(s_won) / len(sd) * 100 if sd else 0,
                "total_won_value": sum(s_amounts),
                "avg_won_value": mean(s_amounts) if s_amounts else 0,
                "avg_cycle_days": mean(s_cycles) if s_cycles else 0,
            })
        source_metrics.sort(key=lambda x: x["total_deals"], reverse=True)
        metrics["source_analysis"] = source_metrics

    # --- Loss Reasons ---
    if "loss_reasons" in focus:
        reason_prop = None
        for prop_name in ["closed_lost_reason", "closed_lost_reason_select", "hs_closed_lost_reason", "lost_reason"]:
            if any(d["raw_props"].get(prop_name) for d in lost):
                reason_prop = prop_name
                break

        if reason_prop:
            reasons_raw = Counter()
            for d in lost:
                r = d["raw_props"].get(reason_prop, "Not specified") or "Not specified"
                reasons_raw[r] += 1
            # Cap to top 10 reasons, group the rest as "Other"
            top_reasons = reasons_raw.most_common(10)
            top_count = sum(c for _, c in top_reasons)
            other_count = sum(reasons_raw.values()) - top_count
            reasons = dict(top_reasons)
            if other_count > 0:
                other_n = len(reasons_raw) - len(top_reasons)
                reasons[f"Other ({other_n} reasons)"] = other_count
            metrics["loss_reasons"] = {
                "property": reason_prop,
                "reasons": reasons,
            }
        else:
            metrics["loss_reasons"] = {"property": None, "reasons": {}}

        # Loss reasons by deal size tier
        if metrics.get("win_rate_by_tier") and reason_prop:
            thresholds = metrics["win_rate_by_tier"]["thresholds"]
            loss_by_tier = {"Small": Counter(), "Mid-Market": Counter(), "Large": Counter()}
            for d in lost:
                if d["amount"] is None:
                    continue
                if d["amount"] <= thresholds["small_max"]:
                    tier = "Small"
                elif d["amount"] <= thresholds["mid_max"]:
                    tier = "Mid-Market"
                else:
                    tier = "Large"
                r = d["raw_props"].get(reason_prop, "Not specified") or "Not specified"
                loss_by_tier[tier][r] += 1
            metrics["loss_reasons_by_tier"] = {t: dict(c.most_common()) for t, c in loss_by_tier.items()}

        # Stage drop-off
        stage_labels = {}
        for st in pipeline_stages:
            stage_labels[st["id"]] = st["label"]
        stage_losses = Counter()
        for d in lost:
            stage_losses[stage_labels.get(d["stage"], d["stage"])] += 1
        metrics["stage_dropoff"] = dict(stage_losses.most_common())

    # --- Deal-level list for drill-down (sanitized, no raw_props) ---
    # Determine tier labels if thresholds exist
    tier_thresholds = None
    if metrics.get("win_rate_by_tier"):
        tier_thresholds = metrics["win_rate_by_tier"]["thresholds"]

    deals_list = []
    for d in parsed:
        tier = None
        if tier_thresholds and d["amount"] is not None and d["amount"] > 0:
            if d["amount"] <= tier_thresholds["small_max"]:
                tier = "Small"
            elif d["amount"] <= tier_thresholds["mid_max"]:
                tier = "Mid-Market"
            else:
                tier = "Large"
        deals_list.append({
            "id": d["id"],
            "name": d["name"],
            "amount": d["amount"],
            "is_won": d["is_won"],
            "is_lost": d["is_lost"],
            "source": d["source"],
            "owner_id": d["owner_id"],
            "owner_name": owners_map.get(d["owner_id"], d["owner_id"] or "Unassigned"),
            "cycle_days": d["cycle_days"],
            "close_date": d["close_date"].strftime("%Y-%m-%d") if d["close_date"] else None,
            "close_month": d["close_month"],
            "close_quarter": d["close_quarter"],
            "tier": tier,
        })
    metrics["deals_list"] = deals_list

    return metrics


def _gini(values):
    """Compute Gini coefficient for revenue concentration."""
    if not values or len(values) < 2:
        return 0
    n = len(values)
    sorted_vals = sorted(values)
    cumulative = sum((i + 1) * v for i, v in enumerate(sorted_vals))
    return (2 * cumulative) / (n * sum(sorted_vals)) - (n + 1) / n


def _safe_float(val):
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _parse_date(val):
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# PDF Report Generator (Morph-branded)
# ---------------------------------------------------------------------------

class ReportBuilder:
    """Generates a professional PDF report with Morph Data Strategies branding."""

    # Morph brand palette (adapted for PDF — light background variant)
    ACCENT = (11, 153, 217)          # #0b99d9 — primary brand blue
    ACCENT_LIGHT = (59, 130, 196)    # #3b82c4
    ACCENT_PALE = (230, 244, 252)    # pale blue for card backgrounds
    ACCENT_WASH = (240, 249, 255)    # ultra-light blue wash
    CHARCOAL = (9, 9, 11)           # #09090b — near-black headings
    DARK_TEXT = (28, 28, 35)         # primary body text
    MID_TEXT = (92, 92, 106)         # #5c5c6a — muted text
    LIGHT_BORDER = (220, 220, 230)   # subtle borders
    SURFACE = (248, 248, 252)        # card surface
    WHITE = (255, 255, 255)
    SUCCESS = (16, 163, 127)         # teal-green for wins
    DANGER = (220, 53, 69)           # red for losses
    WARNING = (245, 166, 35)         # amber for caution
    PURPLE = (111, 68, 196)          # accent for velocity/special

    # Chart colors (hex)
    CH_ACCENT = "#0b99d9"
    CH_ACCENT_LIGHT = "#3b82c4"
    CH_SUCCESS = "#10a37f"
    CH_DANGER = "#dc3545"
    CH_WARNING = "#f5a623"
    CH_PURPLE = "#6f44c4"
    CH_CHARCOAL = "#09090b"
    CH_MID = "#5c5c6a"
    CH_PALE = "#e6f4fc"

    def __init__(self, metrics, config, pipeline_stages, owners_map):
        from fpdf import FPDF

        self.metrics = metrics
        self.config = config
        self.pipeline_stages = pipeline_stages
        self.owners_map = owners_map
        self.chart_files = []

        # Load fonts — prefer clean sans-serif
        font_candidates = [
            ("C:/Windows/Fonts/calibri.ttf", "C:/Windows/Fonts/calibrib.ttf", "C:/Windows/Fonts/calibrii.ttf"),
            ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/ariali.ttf"),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"),
        ]
        font_loaded = False
        font_name = "Helvetica"
        for regular, bold, italic in font_candidates:
            if os.path.exists(regular) and os.path.exists(bold):
                font_name = "MorphFont"
                font_loaded = True
                self._font_files = (regular, bold, italic if os.path.exists(italic) else None)
                break

        self.font = font_name

        # Create FPDF subclass with footer for page numbering
        builder = self

        class MorphPDF(FPDF):
            def footer(this):
                if this.page_no() == 1:
                    return  # no page number on title page
                this.set_y(-15)
                this.set_font(builder.font, "", 7)
                this.set_text_color(*ReportBuilder.MID_TEXT)
                this.cell(0, 8, f"Page {this.page_no()} of {{nb}}", align="C")

        self.pdf = MorphPDF()
        self.pdf.alias_nb_pages()
        self.pdf.set_auto_page_break(auto=True, margin=25)

        if font_loaded:
            regular, bold, italic = self._font_files
            self.pdf.add_font("MorphFont", "", regular)
            self.pdf.add_font("MorphFont", "B", bold)
            if italic:
                self.pdf.add_font("MorphFont", "I", italic)

    def _t(self, text):
        """Sanitize text for PDF output."""
        result = str(text).replace('\n', ' ').replace('\r', '')
        if self.font != "Helvetica":
            return result
        replacements = {
            '\u2013': '-', '\u2014': '-', '\u2018': "'", '\u2019': "'",
            '\u201c': '"', '\u201d': '"', '\u2026': '...', '\u00a0': ' ',
        }
        for char, repl in replacements.items():
            result = result.replace(char, repl)
        return result.encode('latin-1', errors='replace').decode('latin-1')

    def _setup_chart_style(self):
        """Configure matplotlib for polished, modern Morph-branded charts."""
        import matplotlib.pyplot as plt
        import matplotlib as mpl

        mpl.rcParams.update({
            'font.family': 'sans-serif',
            'font.sans-serif': ['Calibri', 'Arial', 'DejaVu Sans', 'Helvetica'],
            'font.size': 9,
            'axes.titlesize': 11,
            'axes.titleweight': 'bold',
            'axes.titlepad': 14,
            'axes.labelsize': 9,
            'axes.labelpad': 8,
            'axes.spines.top': False,
            'axes.spines.right': False,
            'axes.spines.left': True,
            'axes.spines.bottom': True,
            'axes.edgecolor': '#e0e0e8',
            'axes.linewidth': 0.6,
            'axes.labelcolor': '#5c5c6a',
            'axes.titlecolor': '#1c1c23',
            'axes.facecolor': '#fafbfd',
            'xtick.color': '#5c5c6a',
            'ytick.color': '#5c5c6a',
            'xtick.labelsize': 7.5,
            'ytick.labelsize': 7.5,
            'xtick.major.size': 0,
            'ytick.major.size': 0,
            'xtick.major.pad': 6,
            'ytick.major.pad': 6,
            'grid.alpha': 0.4,
            'grid.color': '#e8e8f0',
            'grid.linestyle': '-',
            'grid.linewidth': 0.5,
            'figure.facecolor': 'white',
            'figure.dpi': 120,
            'legend.fontsize': 7.5,
            'legend.framealpha': 0.95,
            'legend.edgecolor': '#e0e0e8',
            'legend.fancybox': True,
            'legend.shadow': False,
            'legend.borderpad': 0.6,
            'savefig.pad_inches': 0.2,
        })

    def build(self, output_path):
        """Build the full PDF report, dynamically adapting sections based on data quality."""
        import matplotlib
        matplotlib.use("Agg")
        self._setup_chart_style()

        es = self.metrics["executive_summary"]
        total = es["total_deals"]

        self._title_page()
        self._executive_summary()
        self._win_loss_deep_dive()

        # Deal size sections: only if we have deals with amounts
        ds = self.metrics.get("deal_size_distribution")
        if ds and ds["count"] >= 5:
            self._deal_size_distribution()
        elif ds:
            print(f"  Skipping deal size distribution (only {ds['count']} deals with amounts)", file=sys.stderr)

        # Tier analysis: need enough deals per tier to be meaningful
        wrt = self.metrics.get("win_rate_by_tier")
        if wrt and all(t["total"] >= 3 for t in wrt["tiers"].values()):
            self._win_rate_by_tier()
        elif wrt:
            print("  Skipping tier analysis (insufficient deals per tier)", file=sys.stderr)

        self._sales_cycle_analysis()

        # Scatter plot: need 10+ data points to be readable
        scatter = self.metrics.get("cycle_vs_size", [])
        if len(scatter) >= 10:
            self._cycle_vs_size()

        # Velocity: need won deals with both amount and cycle data
        dv = self.metrics.get("deal_velocity")
        if dv and len(dv["won"]) >= 5:
            self._deal_velocity()

        if self.metrics.get("revenue_concentration"):
            self._revenue_concentration()

        # Time trends: need 3+ months of data for a meaningful trend
        tt = self.metrics.get("time_trends", [])
        if len(tt) >= 3:
            self._time_trends()
        elif tt:
            print(f"  Skipping monthly trends (only {len(tt)} months of data)", file=sys.stderr)

        # Quarterly: need 2+ quarters
        qt = self.metrics.get("quarterly_trends", [])
        if len(qt) >= 2:
            self._quarterly_trends()

        # Rep performance: need 2+ named reps with deals
        reps = self.metrics.get("rep_performance", [])
        named_reps = [r for r in reps if any(c.isalpha() for c in r["owner_name"])]
        if len(named_reps) >= 2:
            self._rep_performance()
        elif named_reps:
            print(f"  Skipping rep performance (only {len(named_reps)} named rep)", file=sys.stderr)

        # Source analysis: need 2+ sources
        sources = self.metrics.get("source_analysis", [])
        if len(sources) >= 2:
            self._source_analysis()

        # Loss reasons: only if there are actual categorized reasons (not all "Not specified")
        lr = self.metrics.get("loss_reasons")
        if lr and lr.get("reasons"):
            real_reasons = {k: v for k, v in lr["reasons"].items() if k.lower() != "not specified"}
            if real_reasons:
                self._loss_reasons()
            else:
                print("  Skipping loss reasons (all 'Not specified')", file=sys.stderr)

        if self.metrics.get("stage_dropoff"):
            self._stage_dropoff()

        self._key_findings()

        self.pdf.output(str(output_path))

        for f in self.chart_files:
            try:
                os.unlink(f)
            except OSError:
                pass

        return output_path

    def _key_findings(self):
        """Generate a plain-English Key Findings page summarizing the most important patterns."""
        self._section_header("Key Findings", "What your data is telling you")

        es = self.metrics["executive_summary"]
        findings = []

        # Win rate context
        wr = es["win_rate"]
        if wr >= 40:
            findings.append(f"You're closing at {wr:.0f}% — above the typical B2B benchmark of 20-30%. Your qualification process is working.")
        elif wr >= 20:
            findings.append(f"Your {wr:.0f}% win rate is in the normal B2B range (20-30%), but there's room to improve through better qualification or deal strategy.")
        else:
            findings.append(f"At {wr:.0f}%, your win rate suggests deals are entering the pipeline too early or qualification criteria need tightening.")

        # Cycle time insight
        avg_won_cycle = es["avg_won_cycle_days"]
        avg_lost_cycle = es["avg_lost_cycle_days"]
        if avg_won_cycle > 0 and avg_lost_cycle > 0:
            if avg_lost_cycle > avg_won_cycle * 1.5:
                findings.append(
                    f"Wins close in {avg_won_cycle:.0f} days on average, but losses drag to {avg_lost_cycle:.0f} days. "
                    f"Deals still open past {avg_won_cycle * 1.5:.0f} days are statistically much more likely to be lost — consider a formal stale deal review at that mark."
                )
            else:
                findings.append(
                    f"Your average sales cycle is {avg_won_cycle:.0f} days for wins and {avg_lost_cycle:.0f} days for losses. "
                    f"The gap is relatively small, meaning you're qualifying out quickly when deals don't fit."
                )

        # Deal size insight
        avg_won = es["avg_won_deal_size"]
        avg_lost = es["avg_lost_deal_size"]
        median_won = es["median_won_deal_size"]
        if avg_won > 0 and avg_lost > 0:
            if avg_won > avg_lost * 1.5:
                findings.append(
                    f"Won deals average ${avg_won:,.0f} vs ${avg_lost:,.0f} for losses. "
                    f"You win bigger deals more often — your value prop resonates more at higher price points."
                )
            elif avg_lost > avg_won * 1.3:
                findings.append(
                    f"Lost deals average ${avg_lost:,.0f} vs ${avg_won:,.0f} for wins. "
                    f"You may be losing on price at the higher end — consider whether your packaging supports larger deals."
                )

        # Revenue concentration
        rc = self.metrics.get("revenue_concentration")
        if rc:
            pct_deals = rc["pct_deals_for_80"]
            if pct_deals <= 30:
                findings.append(
                    f"{rc['deals_for_80_pct']} deals ({pct_deals:.0f}% of your wins) account for 80% of revenue. "
                    f"High concentration — losing even one large deal has outsized impact on your number."
                )
            else:
                findings.append(
                    f"Revenue is relatively well-distributed across your wins. No single deal represents disproportionate risk."
                )

        # Tier insight
        wrt = self.metrics.get("win_rate_by_tier")
        if wrt and wrt.get("tiers"):
            tiers = wrt["tiers"]
            best_tier = max(tiers.items(), key=lambda x: x[1]["win_rate"])
            worst_tier = min(tiers.items(), key=lambda x: x[1]["win_rate"])
            if best_tier[1]["win_rate"] - worst_tier[1]["win_rate"] > 15:
                findings.append(
                    f"Your win rate varies significantly by deal size: {best_tier[0]} deals close at {best_tier[1]['win_rate']:.0f}% "
                    f"while {worst_tier[0]} deals close at only {worst_tier[1]['win_rate']:.0f}%. "
                    f"Your sweet spot is the ${best_tier[1]['range_low']:,.0f}-${best_tier[1]['range_high']:,.0f} range."
                )

        # Source insight
        sources = self.metrics.get("source_analysis", [])
        if len(sources) >= 2:
            top_source = max(sources, key=lambda s: s.get("win_rate", 0))
            if top_source.get("win_rate", 0) > wr + 10 and top_source.get("total", 0) >= 3:
                findings.append(
                    f"'{top_source['source']}' leads close at {top_source['win_rate']:.0f}% — "
                    f"significantly above your {wr:.0f}% average. Consider investing more in this channel."
                )

        # Loss reasons
        lr = self.metrics.get("loss_reasons")
        if lr and lr.get("reasons"):
            real_reasons = {k: v for k, v in lr["reasons"].items() if k.lower() not in ("not specified", "unknown", "")}
            if real_reasons:
                top_reason = max(real_reasons.items(), key=lambda x: x[1])
                total_categorized = sum(real_reasons.values())
                pct = top_reason[1] / total_categorized * 100
                findings.append(
                    f"'{top_reason[0]}' is your #1 loss reason at {pct:.0f}% of categorized losses. "
                    f"This is the single highest-leverage area to address."
                )

        # Render findings
        self.pdf.set_font(self.font, "", 10)
        self.pdf.set_text_color(*self.CHARCOAL)
        y = self.pdf.get_y() + 8

        for i, finding in enumerate(findings, 1):
            self.pdf.set_xy(16, y)
            self.pdf.set_font(self.font, "B", 10)
            self.pdf.set_text_color(*self.ACCENT)
            self.pdf.cell(8, 7, self._t(f"{i}."))
            self.pdf.set_font(self.font, "", 9.5)
            self.pdf.set_text_color(*self.CHARCOAL)
            self.pdf.set_x(24)
            self.pdf.multi_cell(170, 5.5, self._t(finding))
            y = self.pdf.get_y() + 6

            if y > 260:
                self._new_page()
                y = self.pdf.get_y() + 5

    def _new_page(self):
        self.pdf.add_page()
        # Subtle top accent line on every page (except title — handled separately)
        if self.pdf.page_no() > 1:
            self.pdf.set_fill_color(*self.ACCENT)
            self.pdf.rect(0, 0, 210, 1.2, style="F")

    def _section_header(self, title, subtitle=None):
        """Bold section header with accent block and generous spacing."""
        self._new_page()
        # Accent block background
        self.pdf.set_fill_color(*self.ACCENT)
        self.pdf.rect(0, 1.2, 210, 30, style="F")
        # Lighter fade strip
        r = int(self.ACCENT[0] * 0.7 + 255 * 0.3)
        g = int(self.ACCENT[1] * 0.7 + 255 * 0.3)
        b = int(self.ACCENT[2] * 0.7 + 255 * 0.3)
        self.pdf.set_fill_color(r, g, b)
        self.pdf.rect(0, 31.2, 210, 2, style="F")

        # Title text on accent
        self.pdf.set_xy(14, 6)
        self.pdf.set_font(self.font, "B", 19)
        self.pdf.set_text_color(*self.WHITE)
        self.pdf.cell(0, 10, self._t(title))
        if subtitle:
            self.pdf.set_xy(14, 18)
            self.pdf.set_font(self.font, "", 9)
            self.pdf.set_text_color(210, 235, 252)
            self.pdf.cell(0, 7, self._t(subtitle))

        self.pdf.set_y(40)

    def _sub_header(self, title):
        y = self.pdf.get_y()
        if y > 260:
            self._new_page()
            y = self.pdf.get_y()
        # Small accent dot
        self.pdf.set_fill_color(*self.ACCENT)
        self.pdf.rect(10, y + 3, 3, 3, style="F")
        self.pdf.set_xy(16, y)
        self.pdf.set_font(self.font, "B", 12)
        self.pdf.set_text_color(*self.CHARCOAL)
        self.pdf.cell(0, 10, self._t(title))
        self.pdf.set_y(y + 12)

    def _body_text(self, text):
        self.pdf.set_font(self.font, "", 9.5)
        self.pdf.set_text_color(*self.DARK_TEXT)
        self.pdf.multi_cell(0, 5, self._t(text))
        self.pdf.ln(3)

    def _spacer(self, h=6):
        self.pdf.ln(h)

    def _insight_box(self, text, style="info"):
        """Rounded callout box for key insights with icon-like accent."""
        colors = {
            "info": (self.ACCENT_PALE, self.ACCENT, self.CHARCOAL),
            "success": ((228, 250, 239), self.SUCCESS, self.CHARCOAL),
            "warning": ((255, 245, 228), self.WARNING, self.CHARCOAL),
            "danger": ((254, 232, 234), self.DANGER, self.CHARCOAL),
        }
        icons = {"info": "i", "success": "+", "warning": "!", "danger": "x"}
        bg, bar, txt = colors.get(style, colors["info"])

        y = self.pdf.get_y()
        if y > 245:
            self._new_page()
            y = self.pdf.get_y()

        # Measure text height
        self.pdf.set_font(self.font, "", 8.5)
        safe_text = self._t(text)
        # Use get_string_width to estimate lines
        line_w = 166
        words = safe_text.split(' ')
        lines = 1
        current_w = 0
        for word in words:
            w = self.pdf.get_string_width(word + ' ')
            if current_w + w > line_w:
                lines += 1
                current_w = w
            else:
                current_w += w
        box_h = max(16, lines * 4.5 + 10)

        # Rounded background
        self.pdf.set_fill_color(*bg)
        self.pdf.set_draw_color(*bar)
        self.pdf.rect(12, y, 186, box_h, style="FD", round_corners=True, corner_radius=3)

        # Accent circle with icon
        cx = 20
        cy = y + box_h / 2
        self.pdf.set_fill_color(*bar)
        self.pdf.ellipse(cx - 4, cy - 4, 8, 8, style="F")
        self.pdf.set_font(self.font, "B", 7)
        self.pdf.set_text_color(*self.WHITE)
        self.pdf.set_xy(cx - 4, cy - 3.5)
        self.pdf.cell(8, 7, icons.get(style, "i"), align="C")

        # Text
        self.pdf.set_xy(30, y + 4)
        self.pdf.set_font(self.font, "", 8.5)
        self.pdf.set_text_color(*txt)
        self.pdf.multi_cell(164, 4.5, safe_text)
        self.pdf.set_y(y + box_h + 5)

    def _kpi_cards(self, kpis):
        """Render a row of rounded KPI cards with accent top border."""
        n = len(kpis)
        if n == 0:
            return
        gap = 4
        card_w = (190 - (n - 1) * gap) / n
        y_start = self.pdf.get_y()

        if y_start > 248:
            self._new_page()
            y_start = self.pdf.get_y()

        card_h = 32

        for i, kpi in enumerate(kpis):
            label = kpi[0]
            value = kpi[1]
            delta = kpi[2] if len(kpi) > 2 else None

            x = 10 + i * (card_w + gap)

            # Card shadow (slight offset darker rect)
            self.pdf.set_fill_color(230, 230, 238)
            self.pdf.rect(x + 0.5, y_start + 0.5, card_w, card_h,
                          style="F", round_corners=True, corner_radius=3)

            # Card body
            self.pdf.set_fill_color(*self.WHITE)
            self.pdf.set_draw_color(*self.LIGHT_BORDER)
            self.pdf.rect(x, y_start, card_w, card_h,
                          style="FD", round_corners=True, corner_radius=3)

            # Top accent bar (clipped inside rounded corners)
            self.pdf.set_fill_color(*self.ACCENT)
            self.pdf.rect(x + 1, y_start + 1, card_w - 2, 2.5, style="F")

            # Value
            self.pdf.set_xy(x + 2, y_start + 6)
            self.pdf.set_font(self.font, "B", 15)
            self.pdf.set_text_color(*self.CHARCOAL)
            self.pdf.cell(card_w - 4, 9, self._t(value), align="C")

            # Label
            self.pdf.set_xy(x + 2, y_start + 17)
            self.pdf.set_font(self.font, "", 6.5)
            self.pdf.set_text_color(*self.MID_TEXT)
            self.pdf.cell(card_w - 4, 5, self._t(label), align="C")

            # Delta indicator
            if delta:
                self.pdf.set_xy(x + 2, y_start + 23)
                self.pdf.set_font(self.font, "B", 6.5)
                if delta.startswith("+") or delta.startswith("^"):
                    self.pdf.set_text_color(*self.SUCCESS)
                elif delta.startswith("-") or delta.startswith("v"):
                    self.pdf.set_text_color(*self.DANGER)
                else:
                    self.pdf.set_text_color(*self.MID_TEXT)
                self.pdf.cell(card_w - 4, 4, self._t(delta), align="C")

        self.pdf.set_y(y_start + card_h + 7)

    def _data_table(self, headers, rows, col_widths=None, highlight_col=None):
        """Clean, modern data table with rounded header and subtle striping."""
        if not col_widths:
            col_widths = [190 / len(headers)] * len(headers)

        y = self.pdf.get_y()
        if y > 252:
            self._new_page()

        total_w = sum(col_widths)
        x_start = 10

        # Header row with rounded top
        self.pdf.set_fill_color(*self.CHARCOAL)
        header_y = self.pdf.get_y()
        self.pdf.rect(x_start, header_y, total_w, 9, style="F",
                      round_corners=True, corner_radius=2)
        # Square off bottom corners of header
        self.pdf.rect(x_start, header_y + 4, total_w, 5, style="F")

        self.pdf.set_font(self.font, "B", 7.5)
        self.pdf.set_text_color(200, 210, 220)
        x = x_start
        for i, h in enumerate(headers):
            self.pdf.set_xy(x, header_y + 1)
            self.pdf.cell(col_widths[i], 7, self._t(h), align="C" if i > 0 else "L")
            x += col_widths[i]
        self.pdf.set_y(header_y + 9)

        # Data rows
        self.pdf.set_font(self.font, "", 7.5)
        row_h = 7
        for row_idx, row in enumerate(rows):
            # Page break with header re-draw
            if self.pdf.get_y() > 265:
                self._new_page()
                self.pdf.set_fill_color(*self.CHARCOAL)
                hy = self.pdf.get_y()
                self.pdf.rect(x_start, hy, total_w, 9, style="F",
                              round_corners=True, corner_radius=2)
                self.pdf.rect(x_start, hy + 4, total_w, 5, style="F")
                self.pdf.set_font(self.font, "B", 7.5)
                self.pdf.set_text_color(200, 210, 220)
                x = x_start
                for i, h in enumerate(headers):
                    self.pdf.set_xy(x, hy + 1)
                    self.pdf.cell(col_widths[i], 7, self._t(h), align="C" if i > 0 else "L")
                    x += col_widths[i]
                self.pdf.set_y(hy + 9)
                self.pdf.set_font(self.font, "", 7.5)

            if row_idx % 2 == 0:
                self.pdf.set_fill_color(246, 248, 252)
            else:
                self.pdf.set_fill_color(*self.WHITE)

            ry = self.pdf.get_y()
            self.pdf.rect(x_start, ry, total_w, row_h, style="F")

            self.pdf.set_text_color(*self.DARK_TEXT)
            x = x_start
            for i, cell in enumerate(row):
                self.pdf.set_xy(x, ry)
                if highlight_col is not None and i == highlight_col:
                    self.pdf.set_font(self.font, "B", 7.5)
                    self.pdf.set_text_color(*self.ACCENT)
                self.pdf.cell(col_widths[i], row_h, self._t(cell),
                              align="C" if i > 0 else "L")
                if highlight_col is not None and i == highlight_col:
                    self.pdf.set_font(self.font, "", 7.5)
                    self.pdf.set_text_color(*self.DARK_TEXT)
                x += col_widths[i]
            self.pdf.set_y(ry + row_h)

        # Bottom rounded edge
        by = self.pdf.get_y()
        self.pdf.set_draw_color(*self.LIGHT_BORDER)
        self.pdf.set_line_width(0.3)
        self.pdf.rect(x_start, by - 0.5, total_w, 1.5, style="F",
                      round_corners=True, corner_radius=1)
        self.pdf.set_fill_color(*self.WHITE)
        self.pdf.set_line_width(0.2)
        self.pdf.ln(6)

    def _embed_chart(self, fig, width=180):
        """Save matplotlib figure to temp file and embed in PDF with subtle frame."""
        if self.pdf.get_y() > 165:
            self._new_page()

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        fig.savefig(tmp.name, dpi=200, bbox_inches="tight", facecolor="white",
                    edgecolor="none", pad_inches=0.2)
        tmp.close()
        self.chart_files.append(tmp.name)

        import matplotlib.pyplot as plt
        plt.close(fig)

        x = (210 - width) / 2
        y = self.pdf.get_y()

        # Subtle card wrapper around chart
        self.pdf.set_fill_color(252, 252, 255)
        self.pdf.set_draw_color(*self.LIGHT_BORDER)
        # Estimate image height (approx based on width and typical aspect ratio)
        self.pdf.image(tmp.name, x=x, w=width)
        self.pdf.ln(6)

    # ===================================================================
    # Report sections
    # ===================================================================

    def _title_page(self):
        self.pdf.add_page()  # Don't use _new_page — no accent line on title

        # Full-bleed accent header block (top third of page)
        self.pdf.set_fill_color(*self.ACCENT)
        self.pdf.rect(0, 0, 210, 95, style="F")

        # Clean bottom edge — short 4-step fade instead of long cascade
        steps = 4
        for i in range(steps):
            t = (i + 1) / steps
            r = int(self.ACCENT[0] + (255 - self.ACCENT[0]) * t)
            g = int(self.ACCENT[1] + (255 - self.ACCENT[1]) * t)
            b = int(self.ACCENT[2] + (255 - self.ACCENT[2]) * t)
            self.pdf.set_fill_color(r, g, b)
            self.pdf.rect(0, 95 + i * 1.5, 210, 1.5, style="F")


        # Title text on accent
        self.pdf.set_xy(16, 38)
        self.pdf.set_font(self.font, "B", 34)
        self.pdf.set_text_color(*self.WHITE)
        self.pdf.cell(0, 16, self._t("Deal Analysis"))
        self.pdf.set_xy(16, 58)
        self.pdf.set_font(self.font, "", 14)
        self.pdf.set_text_color(210, 235, 252)
        self.pdf.cell(0, 10, self._t("Performance & Pipeline Intelligence"))

        # Date range badge on accent
        tp = self.config.get("time_period", {})
        if tp.get("start") and tp.get("end"):
            date_range = f"{tp['start']}  to  {tp['end']}"
        elif tp.get("start"):
            date_range = f"From {tp['start']}"
        elif tp.get("end"):
            date_range = f"Through {tp['end']}"
        else:
            date_range = "All Time"

        self.pdf.set_xy(16, 76)
        self.pdf.set_font(self.font, "", 10)
        self.pdf.set_text_color(180, 220, 245)
        # Date badge with rounded background
        badge_w = self.pdf.get_string_width(date_range) + 14
        self.pdf.set_fill_color(int(self.ACCENT[0] * 0.8), int(self.ACCENT[1] * 0.8), int(self.ACCENT[2] * 0.8))
        self.pdf.rect(16, 76, badge_w, 10, style="F", round_corners=True, corner_radius=4)
        self.pdf.set_xy(16, 76)
        self.pdf.set_text_color(220, 240, 255)
        self.pdf.cell(badge_w, 10, self._t(date_range), align="C")

        # Key stats below the gradient — on white background
        es = self.metrics["executive_summary"]

        # Three stat blocks centered
        stat_y = 135
        block_w = 55
        gap = 8
        total_stat_w = block_w * 3 + gap * 2
        start_x = (210 - total_stat_w) / 2

        stats = [
            (f"{es['total_deals']}", "DEALS ANALYZED", self.CHARCOAL),
            (f"{es['won_count']}", "WON", self.SUCCESS),
            (f"{es['lost_count']}", "LOST", self.DANGER),
        ]

        for idx, (val, label, color) in enumerate(stats):
            bx = start_x + idx * (block_w + gap)

            # Card with shadow
            self.pdf.set_fill_color(235, 235, 242)
            self.pdf.rect(bx + 0.8, stat_y + 0.8, block_w, 38,
                          style="F", round_corners=True, corner_radius=4)
            self.pdf.set_fill_color(*self.WHITE)
            self.pdf.set_draw_color(*self.LIGHT_BORDER)
            self.pdf.rect(bx, stat_y, block_w, 38,
                          style="FD", round_corners=True, corner_radius=4)

            # Accent top
            self.pdf.set_fill_color(*color)
            self.pdf.rect(bx + 4, stat_y + 3, block_w - 8, 2, style="F",
                          round_corners=True, corner_radius=1)

            # Value
            self.pdf.set_xy(bx, stat_y + 8)
            self.pdf.set_font(self.font, "B", 26)
            self.pdf.set_text_color(*color)
            self.pdf.cell(block_w, 14, self._t(val), align="C")

            # Label
            self.pdf.set_xy(bx, stat_y + 24)
            self.pdf.set_font(self.font, "", 7)
            self.pdf.set_text_color(*self.MID_TEXT)
            self.pdf.cell(block_w, 6, self._t(label), align="C")

        # Win rate highlight below stats
        wr_y = stat_y + 48
        self.pdf.set_font(self.font, "B", 18)
        self.pdf.set_text_color(*self.ACCENT)
        self.pdf.set_xy(0, wr_y)
        self.pdf.cell(210, 12, self._t(f"{es['win_rate']:.1f}% Win Rate"), align="C")
        self.pdf.set_xy(0, wr_y + 12)
        self.pdf.set_font(self.font, "", 9)
        self.pdf.set_text_color(*self.MID_TEXT)
        self.pdf.cell(210, 6, self._t(
            f"${es['total_won_value']:,.0f} total revenue won  |  "
            f"${es['avg_won_deal_size']:,.0f} avg deal size  |  "
            f"{es['avg_won_cycle_days']:.0f} day avg cycle"
        ), align="C")

        # Footer — disable auto page break to prevent bleed
        self.pdf.set_auto_page_break(False)
        footer_y = 280
        self.pdf.set_draw_color(*self.LIGHT_BORDER)
        self.pdf.line(60, footer_y, 150, footer_y)
        self.pdf.set_font(self.font, "", 7.5)
        self.pdf.set_text_color(*self.MID_TEXT)
        gen_text = f"Generated {datetime.now().strftime('%B %d, %Y at %I:%M %p')}"
        powered_text = "Powered by Morph Data Strategies"
        full_footer = f"{gen_text}  |  {powered_text}"
        text_w = self.pdf.get_string_width(full_footer)
        footer_x = (210 - text_w) / 2
        self.pdf.text(footer_x, footer_y + 6, self._t(gen_text + "  |  "))
        # "Powered by" as a clickable link
        link_x = footer_x + self.pdf.get_string_width(gen_text + "  |  ")
        self.pdf.set_text_color(*self.ACCENT)
        self.pdf.text(link_x, footer_y + 6, self._t(powered_text))
        self.pdf.link(link_x, footer_y + 2, self.pdf.get_string_width(powered_text), 6,
                      "https://morphdatastrategies.com")

        # Bottom accent bar
        self.pdf.set_fill_color(*self.ACCENT)
        self.pdf.rect(0, 293, 210, 4, style="F")
        self.pdf.set_auto_page_break(True, margin=15)

    def _executive_summary(self):
        es = self.metrics["executive_summary"]
        self._section_header("Executive Summary", "High-level performance snapshot across all analyzed deals")

        self._kpi_cards([
            ("TOTAL DEALS", f"{es['total_deals']}"),
            ("WIN RATE", f"{es['win_rate']:.1f}%"),
            ("TOTAL REVENUE WON", f"${es['total_won_value']:,.0f}"),
            ("REVENUE LOST", f"${es['total_lost_value']:,.0f}"),
        ])

        self._kpi_cards([
            ("AVG WON DEAL", f"${es['avg_won_deal_size']:,.0f}"),
            ("MEDIAN WON DEAL", f"${es['median_won_deal_size']:,.0f}"),
            ("AVG CYCLE (WON)", f"{es['avg_won_cycle_days']:.0f} days"),
            ("AVG CYCLE (LOST)", f"{es['avg_lost_cycle_days']:.0f} days"),
        ])

        # Generate insight
        insights = []
        if es['win_rate'] < 25:
            insights.append(f"Win rate of {es['win_rate']:.1f}% is below typical B2B benchmarks (25-35%). This may indicate qualification issues early in the pipeline, or deals entering the pipeline that aren't a strong fit.")
        elif es['win_rate'] > 50:
            insights.append(f"Win rate of {es['win_rate']:.1f}% is strong. The team is converting at an above-average clip, which may indicate either strong qualification discipline or a mature sales process.")
        else:
            insights.append(f"Win rate of {es['win_rate']:.1f}% is within the typical B2B range. Opportunities for improvement likely exist in pipeline qualification and deal execution.")

        if es['avg_won_deal_size'] > 0 and es['avg_lost_deal_size'] > 0:
            ratio = es['avg_won_deal_size'] / es['avg_lost_deal_size']
            if ratio > 1.3:
                insights.append(f"Won deals are {ratio:.1f}x larger than lost deals on average (${es['avg_won_deal_size']:,.0f} vs ${es['avg_lost_deal_size']:,.0f}), suggesting the team performs better on larger opportunities.")
            elif ratio < 0.7:
                insights.append(f"Lost deals average ${es['avg_lost_deal_size']:,.0f} vs ${es['avg_won_deal_size']:,.0f} for wins. Larger deals are harder to close - consider dedicated strategies for enterprise-tier opportunities.")

        if es['avg_won_cycle_days'] > 0 and es['avg_lost_cycle_days'] > 0:
            if es['avg_lost_cycle_days'] > es['avg_won_cycle_days'] * 1.5:
                insights.append(f"Lost deals drag on {es['avg_lost_cycle_days']:.0f} days vs {es['avg_won_cycle_days']:.0f} for wins. Implementing stricter deal stage exit criteria could free up rep bandwidth faster.")

        for insight in insights:
            self._insight_box(insight)

    def _win_loss_deep_dive(self):
        import matplotlib.pyplot as plt

        wl = self.metrics["win_loss"]
        self._section_header("Win / Loss Breakdown", "Volume and value comparison between won and lost deals")

        headers = ["Outcome", "Count", "Total Value", "Avg Deal Size"]
        rows = [
            ["Won", str(wl["won"]["count"]), f"${wl['won']['total_value']:,.0f}", f"${wl['won']['avg_value']:,.0f}"],
            ["Lost", str(wl["lost"]["count"]), f"${wl['lost']['total_value']:,.0f}", f"${wl['lost']['avg_value']:,.0f}"],
        ]
        self._data_table(headers, rows, [45, 35, 55, 55])

        # Dual chart: donut + value comparison
        fig, axes = plt.subplots(1, 2, figsize=(9, 3.5))

        # Donut chart
        sizes = [wl["won"]["count"], wl["lost"]["count"]]
        colors = [self.CH_SUCCESS, self.CH_DANGER]
        wedges, texts, autotexts = axes[0].pie(
            sizes, colors=colors, autopct='%1.1f%%', startangle=90,
            wedgeprops=dict(width=0.4, edgecolor='white', linewidth=2),
            pctdistance=0.78
        )
        for t in autotexts:
            t.set_fontsize(9)
            t.set_fontweight('bold')
        axes[0].legend(["Won", "Lost"], loc="lower center", ncol=2,
                       fontsize=9, frameon=False)
        centre = axes[0].text(0, 0, f"{wl['won']['count'] + wl['lost']['count']}\nDeals",
                              ha='center', va='center', fontsize=14, fontweight='bold',
                              color=self.CH_CHARCOAL)
        axes[0].set_title("Deal Count Split", pad=12)

        # Value comparison bars
        categories = ["Won", "Lost"]
        values = [wl["won"]["total_value"], wl["lost"]["total_value"]]
        bars = axes[1].bar(categories, values, color=colors, width=0.5,
                           edgecolor='white', linewidth=1)
        axes[1].set_title("Total Value Comparison", pad=12)
        axes[1].yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
        axes[1].grid(axis='y', alpha=0.3)
        for bar, val in zip(bars, values):
            axes[1].text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(values) * 0.02,
                         f"${val:,.0f}", ha='center', va='bottom', fontsize=9, fontweight='bold',
                         color=self.CH_CHARCOAL)

        fig.tight_layout(pad=2)
        self._embed_chart(fig)

    def _deal_size_distribution(self):
        import matplotlib.pyplot as plt
        import numpy as np

        ds = self.metrics["deal_size_distribution"]
        self._section_header("Deal Size Distribution", "Understanding your deal value landscape and concentration patterns")

        self._kpi_cards([
            ("MEAN DEAL SIZE", f"${ds['mean']:,.0f}"),
            ("MEDIAN DEAL SIZE", f"${ds['median']:,.0f}"),
            ("INTERQUARTILE RANGE", f"${ds['q1']:,.0f} - ${ds['q3']:,.0f}"),
            ("STANDARD DEVIATION", f"${ds['stdev']:,.0f}"),
        ])

        # Insight on skew
        skew_ratio = ds['mean'] / ds['median'] if ds['median'] > 0 else 1
        if skew_ratio > 1.5:
            self._insight_box(
                f"Deal sizes are right-skewed (mean ${ds['mean']:,.0f} is {skew_ratio:.1f}x the median ${ds['median']:,.0f}). "
                f"A small number of large deals are pulling the average up. The median is a better measure of your 'typical' deal. "
                f"Range spans ${ds['min']:,.0f} to ${ds['max']:,.0f}.",
                "warning"
            )
        elif ds['stdev'] > ds['mean'] * 0.8:
            self._insight_box(
                f"High variance in deal sizes (CV = {ds['stdev']/ds['mean']:.1f}). Consider segmenting your pipeline by deal tier for more targeted forecasting.",
                "warning"
            )

        # Enhanced histogram with KDE-like overlay
        fig, axes = plt.subplots(1, 2, figsize=(9, 3.5))

        won_vals = ds.get("won_values", [])
        lost_vals = ds.get("lost_values", [])

        if won_vals or lost_vals:
            all_vals = won_vals + lost_vals
            bins = min(25, max(8, len(all_vals) // 4))

            axes[0].hist([won_vals, lost_vals], bins=bins, stacked=True,
                         color=[self.CH_SUCCESS, self.CH_DANGER], label=["Won", "Lost"],
                         edgecolor="white", linewidth=0.5, alpha=0.85)
            axes[0].axvline(ds['median'], color=self.CH_ACCENT, linestyle='--', linewidth=1.5,
                            label=f"Median: ${ds['median']:,.0f}")
            axes[0].legend(fontsize=8, loc='upper right')
            axes[0].set_title("Deal Size Histogram", pad=10)
            axes[0].set_xlabel("Deal Amount ($)")
            axes[0].set_ylabel("Count")
            axes[0].xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x/1000:.0f}K" if x >= 1000 else f"${x:.0f}"))
            axes[0].grid(axis='y', alpha=0.3)

        # Box plot comparison
        data_to_plot = []
        labels = []
        colors_bp = []
        if won_vals:
            data_to_plot.append(won_vals)
            labels.append(f"Won (n={len(won_vals)})")
            colors_bp.append(self.CH_SUCCESS)
        if lost_vals:
            data_to_plot.append(lost_vals)
            labels.append(f"Lost (n={len(lost_vals)})")
            colors_bp.append(self.CH_DANGER)

        if data_to_plot:
            bp = axes[1].boxplot(data_to_plot, tick_labels=labels, patch_artist=True,
                                 widths=0.4, showmeans=True,
                                 meanprops=dict(marker='D', markerfacecolor=self.CH_ACCENT, markersize=6))
            for patch, color in zip(bp["boxes"], colors_bp):
                patch.set_facecolor(color)
                patch.set_alpha(0.5)
            for median_line in bp['medians']:
                median_line.set_color(self.CH_CHARCOAL)
                median_line.set_linewidth(2)
            axes[1].set_title("Value Distribution by Outcome", pad=10)
            axes[1].set_ylabel("Deal Amount ($)")
            axes[1].yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x/1000:.0f}K" if x >= 1000 else f"${x:.0f}"))
            axes[1].grid(axis='y', alpha=0.3)

        fig.tight_layout(pad=2)
        self._embed_chart(fig)

    def _win_rate_by_tier(self):
        import matplotlib.pyplot as plt

        wrt = self.metrics["win_rate_by_tier"]
        tiers = wrt["tiers"]
        thresholds = wrt["thresholds"]
        self._section_header("Win Rate by Deal Size Tier",
                             f"Small: <${thresholds['small_max']:,.0f} | Mid-Market: ${thresholds['small_max']:,.0f}-${thresholds['mid_max']:,.0f} | Large: >${thresholds['mid_max']:,.0f}")

        headers = ["Tier", "Deals", "Won", "Lost", "Win Rate", "Avg Size", "Total Won $", "Avg Cycle"]
        rows = []
        for tier_name in ["Small", "Mid-Market", "Large"]:
            t = tiers[tier_name]
            rows.append([
                tier_name,
                str(t["total"]),
                str(t["won"]),
                str(t["lost"]),
                f"{t['win_rate']:.1f}%",
                f"${t['avg_deal_size']:,.0f}",
                f"${t['total_won_value']:,.0f}",
                f"{t['avg_cycle_days']:.0f}d" if t['avg_cycle_days'] > 0 else "N/A",
            ])
        self._data_table(headers, rows, [28, 18, 18, 18, 25, 28, 30, 25], highlight_col=4)

        # Tier comparison chart
        fig, axes = plt.subplots(1, 3, figsize=(9, 3))
        tier_names = ["Small", "Mid-Market", "Large"]
        tier_colors = [self.CH_ACCENT, self.CH_PURPLE, self.CH_WARNING]

        # Win rate bars
        win_rates = [tiers[t]["win_rate"] for t in tier_names]
        bars = axes[0].bar(tier_names, win_rates, color=tier_colors, width=0.5, edgecolor='white')
        axes[0].set_title("Win Rate by Tier", pad=10)
        axes[0].set_ylabel("Win Rate (%)")
        axes[0].set_ylim(0, max(100, max(win_rates) + 10))
        for bar, rate in zip(bars, win_rates):
            axes[0].text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 2,
                         f"{rate:.1f}%", ha='center', fontsize=9, fontweight='bold')
        axes[0].grid(axis='y', alpha=0.3)

        # Revenue by tier
        rev = [tiers[t]["total_won_value"] for t in tier_names]
        bars2 = axes[1].bar(tier_names, rev, color=tier_colors, width=0.5, edgecolor='white')
        axes[1].set_title("Revenue by Tier", pad=10)
        axes[1].yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x/1000:.0f}K" if x >= 1000 else f"${x:.0f}"))
        axes[1].grid(axis='y', alpha=0.3)

        # Cycle time by tier
        cycles = [tiers[t]["avg_cycle_days"] for t in tier_names]
        bars3 = axes[2].bar(tier_names, cycles, color=tier_colors, width=0.5, edgecolor='white')
        axes[2].set_title("Avg Cycle (Won)", pad=10)
        axes[2].set_ylabel("Days")
        for bar, c in zip(bars3, cycles):
            if c > 0:
                axes[2].text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                             f"{c:.0f}d", ha='center', fontsize=9)
        axes[2].grid(axis='y', alpha=0.3)

        fig.tight_layout(pad=2)
        self._embed_chart(fig)

        # Insight
        best_tier = max(tiers.items(), key=lambda x: x[1]["win_rate"])
        worst_tier = min(tiers.items(), key=lambda x: x[1]["win_rate"])
        if best_tier[1]["win_rate"] - worst_tier[1]["win_rate"] > 15:
            self._insight_box(
                f"{best_tier[0]} deals close at {best_tier[1]['win_rate']:.1f}% vs {worst_tier[0]} at {worst_tier[1]['win_rate']:.1f}%. "
                f"The {abs(best_tier[1]['win_rate'] - worst_tier[1]['win_rate']):.0f}pp spread suggests deal execution differs significantly by size. "
                f"Consider tailoring your sales process for each tier.",
                "info"
            )

    def _sales_cycle_analysis(self):
        import matplotlib.pyplot as plt

        sc = self.metrics["sales_cycle"]
        self._section_header("Sales Cycle Analysis", "How long deals take to close and what that tells us")

        self._kpi_cards([
            ("AVG WON CYCLE", f"{sc['avg_won']:.0f} days"),
            ("MEDIAN WON CYCLE", f"{sc['median_won']:.0f} days"),
            ("AVG LOST CYCLE", f"{sc['avg_lost']:.0f} days"),
            ("MEDIAN LOST CYCLE", f"{sc['median_lost']:.0f} days"),
        ])

        won_c = sc["won_cycles"]
        lost_c = sc["lost_cycles"]

        if won_c or lost_c:
            fig, axes = plt.subplots(1, 2, figsize=(9, 3.5))

            # Violin + box combo
            data = []
            labels = []
            colors = []
            if won_c:
                data.append(won_c)
                labels.append(f"Won (n={len(won_c)})")
                colors.append(self.CH_SUCCESS)
            if lost_c:
                data.append(lost_c)
                labels.append(f"Lost (n={len(lost_c)})")
                colors.append(self.CH_DANGER)

            parts = axes[0].violinplot(data, showmeans=True, showmedians=True)
            for i, pc in enumerate(parts['bodies']):
                pc.set_facecolor(colors[i])
                pc.set_alpha(0.4)
            parts['cmeans'].set_color(self.CH_ACCENT)
            parts['cmedians'].set_color(self.CH_CHARCOAL)
            axes[0].set_xticks(range(1, len(labels) + 1))
            axes[0].set_xticklabels(labels)
            axes[0].set_ylabel("Days")
            axes[0].set_title("Cycle Time Distribution", pad=10)
            axes[0].grid(axis='y', alpha=0.3)

            # Histogram overlay
            if won_c and lost_c:
                max_days = max(max(won_c), max(lost_c))
                bins = range(0, int(max_days) + 30, max(7, int(max_days) // 20))
                axes[1].hist(won_c, bins=bins, alpha=0.6, color=self.CH_SUCCESS,
                             label="Won", edgecolor='white', linewidth=0.5)
                axes[1].hist(lost_c, bins=bins, alpha=0.6, color=self.CH_DANGER,
                             label="Lost", edgecolor='white', linewidth=0.5)
                axes[1].axvline(sc['median_won'], color=self.CH_SUCCESS, linestyle='--',
                                linewidth=1.5, label=f"Won median: {sc['median_won']:.0f}d")
                axes[1].axvline(sc['median_lost'], color=self.CH_DANGER, linestyle='--',
                                linewidth=1.5, label=f"Lost median: {sc['median_lost']:.0f}d")
                axes[1].legend(fontsize=7)
                axes[1].set_xlabel("Days")
                axes[1].set_ylabel("Count")
                axes[1].set_title("Cycle Time Histogram", pad=10)
                axes[1].grid(axis='y', alpha=0.3)
            elif won_c:
                axes[1].hist(won_c, bins=20, alpha=0.7, color=self.CH_SUCCESS, edgecolor='white')
                axes[1].set_title("Won Deal Cycle Times", pad=10)
                axes[1].set_xlabel("Days")
                axes[1].grid(axis='y', alpha=0.3)

            fig.tight_layout(pad=2)
            self._embed_chart(fig)

        # Insight
        if sc['avg_won'] > 0 and sc['median_won'] > 0:
            if sc['avg_won'] > sc['median_won'] * 1.5:
                self._insight_box(
                    f"The gap between mean ({sc['avg_won']:.0f}d) and median ({sc['median_won']:.0f}d) cycle time indicates outlier deals dragging the average up. "
                    f"Focus on median for planning. Long-tail deals may need a different closing strategy.",
                    "warning"
                )

    def _cycle_vs_size(self):
        import matplotlib.pyplot as plt

        scatter = self.metrics["cycle_vs_size"]
        if len(scatter) < 5:
            return

        self._section_header("Cycle Time vs Deal Size", "Mapping the relationship between deal value and time to close")

        fig, ax = plt.subplots(figsize=(8, 4.5))

        won = [d for d in scatter if d["is_won"]]
        lost = [d for d in scatter if not d["is_won"]]

        if won:
            ax.scatter([d["cycle_days"] for d in won], [d["amount"] for d in won],
                       c=self.CH_SUCCESS, alpha=0.5, s=40, label="Won", edgecolors='white', linewidth=0.5)
        if lost:
            ax.scatter([d["cycle_days"] for d in lost], [d["amount"] for d in lost],
                       c=self.CH_DANGER, alpha=0.5, s=40, label="Lost", edgecolors='white', linewidth=0.5)

        # Add quadrant lines at medians
        if won:
            med_cycle = median([d["cycle_days"] for d in won])
            med_amount = median([d["amount"] for d in won])
            ax.axhline(med_amount, color=self.CH_MID, linestyle=':', alpha=0.5, linewidth=1)
            ax.axvline(med_cycle, color=self.CH_MID, linestyle=':', alpha=0.5, linewidth=1)
            # Quadrant labels
            max_x = max(d["cycle_days"] for d in scatter)
            max_y = max(d["amount"] for d in scatter)
            ax.text(med_cycle * 0.3, max_y * 0.95, "FAST + LARGE\n(Sweet Spot)", fontsize=7,
                    color=self.CH_SUCCESS, ha='center', style='italic', alpha=0.7)
            ax.text(max_x * 0.85, max_y * 0.95, "SLOW + LARGE\n(Enterprise)", fontsize=7,
                    color=self.CH_WARNING, ha='center', style='italic', alpha=0.7)
            ax.text(med_cycle * 0.3, max_y * 0.05, "FAST + SMALL\n(Quick Wins)", fontsize=7,
                    color=self.CH_ACCENT, ha='center', style='italic', alpha=0.7)
            ax.text(max_x * 0.85, max_y * 0.05, "SLOW + SMALL\n(Drag Zone)", fontsize=7,
                    color=self.CH_DANGER, ha='center', style='italic', alpha=0.7)

        ax.set_xlabel("Cycle Time (Days)")
        ax.set_ylabel("Deal Amount ($)")
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
        ax.legend(loc='upper right', frameon=True)
        ax.set_title("Deal Size vs Cycle Time Quadrant Map", pad=12)
        ax.grid(alpha=0.2)

        fig.tight_layout()
        self._embed_chart(fig)

        # Insight
        if won:
            fast_large = [d for d in won if d["cycle_days"] < med_cycle and d["amount"] > med_amount]
            slow_small = [d for d in won if d["cycle_days"] > med_cycle and d["amount"] < med_amount]
            if fast_large:
                total_fast_large = sum(d["amount"] for d in fast_large)
                self._insight_box(
                    f"{len(fast_large)} deals in the 'Sweet Spot' quadrant (fast + large) generated ${total_fast_large:,.0f} in revenue. "
                    f"Understanding what these deals have in common could reveal your ideal customer profile.",
                    "success"
                )
            if slow_small and len(slow_small) > len(won) * 0.2:
                self._insight_box(
                    f"{len(slow_small)} deals ({len(slow_small)/len(won)*100:.0f}% of wins) sit in the 'Drag Zone' - slow to close and low value. "
                    f"Consider whether these are worth the rep bandwidth.",
                    "danger"
                )

    def _deal_velocity(self):
        import matplotlib.pyplot as plt

        dv = self.metrics["deal_velocity"]
        self._section_header("Deal Velocity Analysis", "Revenue generated per day of sales effort ($/day)")

        self._kpi_cards([
            ("AVG WON VELOCITY", f"${dv['avg_won_velocity']:,.0f}/day"),
            ("AVG LOST VELOCITY", f"${dv['avg_lost_velocity']:,.0f}/day"),
            ("TOP DEALS ANALYZED", f"{len(dv['won'])}"),
        ])

        # Top 10 fastest deals
        top_10 = dv["won"][:10]
        if top_10:
            self._sub_header("Top 10 Highest-Velocity Won Deals")
            headers = ["Deal", "Amount", "Cycle", "Velocity ($/day)"]
            rows = []
            for d in top_10:
                rows.append([
                    d["name"][:30],
                    f"${d['amount']:,.0f}",
                    f"{d['cycle_days']}d",
                    f"${d['velocity']:,.0f}/day",
                ])
            self._data_table(headers, rows, [65, 40, 30, 55], highlight_col=3)

        # Velocity distribution chart
        if len(dv["won"]) > 3:
            fig, ax = plt.subplots(figsize=(8, 3.5))
            velocities = [d["velocity"] for d in dv["won"]]
            bins = min(20, max(5, len(velocities) // 3))
            ax.hist(velocities, bins=bins, color=self.CH_ACCENT, edgecolor='white',
                    linewidth=0.5, alpha=0.8)
            ax.axvline(mean(velocities), color=self.CH_CHARCOAL, linestyle='--',
                       linewidth=1.5, label=f"Mean: ${mean(velocities):,.0f}/day")
            ax.axvline(median(velocities), color=self.CH_SUCCESS, linestyle='--',
                       linewidth=1.5, label=f"Median: ${median(velocities):,.0f}/day")
            ax.legend()
            ax.set_title("Velocity Distribution (Won Deals)", pad=10)
            ax.set_xlabel("Revenue per Day ($/day)")
            ax.set_ylabel("Count")
            ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
            ax.grid(axis='y', alpha=0.3)
            fig.tight_layout()
            self._embed_chart(fig)

    def _revenue_concentration(self):
        import matplotlib.pyplot as plt

        rc = self.metrics["revenue_concentration"]
        self._section_header("Revenue Concentration", "How concentrated is your revenue across deals?")

        self._kpi_cards([
            ("DEALS FOR 80% REVENUE", f"{rc['deals_for_80_pct']}"),
            ("% OF DEALS FOR 80% REV", f"{rc['pct_deals_for_80']:.1f}%"),
            ("TOTAL REVENUE", f"${rc['total_revenue']:,.0f}"),
            ("CONCENTRATION INDEX", f"{rc['gini_coefficient']:.2f}"),
        ])

        # Insight
        if rc['pct_deals_for_80'] < 30:
            self._insight_box(
                f"Revenue is highly concentrated: {rc['pct_deals_for_80']:.0f}% of deals drive 80% of revenue (Gini: {rc['gini_coefficient']:.2f}). "
                f"This creates risk - losing a few large deals could significantly impact targets. "
                f"Diversifying toward more mid-market wins would build a more resilient pipeline.",
                "danger"
            )
        elif rc['pct_deals_for_80'] > 60:
            self._insight_box(
                f"Revenue is well-distributed: {rc['pct_deals_for_80']:.0f}% of deals contribute 80% of revenue (Gini: {rc['gini_coefficient']:.2f}). "
                f"This is a healthy, diversified revenue base with low deal-loss sensitivity.",
                "success"
            )
        else:
            self._insight_box(
                f"{rc['pct_deals_for_80']:.0f}% of deals generate 80% of revenue (Gini: {rc['gini_coefficient']:.2f}). "
                f"Moderate concentration - monitor your top deals closely while building mid-market pipeline.",
                "info"
            )

        # Pareto chart
        fig, ax = plt.subplots(figsize=(8, 4))
        deal_pcts = [p["deal_pct"] for p in rc["pareto_data"]]
        cum_pcts = [p["cumulative_pct"] for p in rc["pareto_data"]]

        ax.fill_between(deal_pcts, cum_pcts, alpha=0.15, color=self.CH_ACCENT)
        ax.plot(deal_pcts, cum_pcts, color=self.CH_ACCENT, linewidth=2.5, label="Actual")
        # Perfect equality line
        ax.plot([0, 100], [0, 100], color=self.CH_MID, linestyle='--', linewidth=1,
                alpha=0.5, label="Perfect Equality")
        # 80% line
        ax.axhline(80, color=self.CH_DANGER, linestyle=':', linewidth=1, alpha=0.6)
        ax.axvline(rc['pct_deals_for_80'], color=self.CH_DANGER, linestyle=':', linewidth=1, alpha=0.6)
        ax.text(rc['pct_deals_for_80'] + 2, 82,
                f"{rc['pct_deals_for_80']:.0f}% of deals = 80% of revenue",
                fontsize=8, color=self.CH_DANGER)

        ax.set_xlabel("% of Deals (ranked by value)")
        ax.set_ylabel("% of Cumulative Revenue")
        ax.set_title("Revenue Concentration Curve (Lorenz)", pad=12)
        ax.set_xlim(0, 100)
        ax.set_ylim(0, 100)
        ax.legend(loc='lower right')
        ax.grid(alpha=0.2)

        fig.tight_layout()
        self._embed_chart(fig)

        # Top 5 deals table
        if rc["top_5_deals"]:
            self._sub_header("Top 5 Revenue-Generating Deals")
            headers = ["Rank", "Deal Name", "Deal Value", "% of Revenue", "Cumulative %"]
            rows = []
            for p in rc["top_5_deals"]:
                rows.append([
                    f"#{p['deal_rank']}",
                    p.get("deal_name", "Unknown")[:35],
                    f"${p['amount']:,.0f}",
                    f"{p['amount'] / rc['total_revenue'] * 100:.1f}%",
                    f"{p['cumulative_pct']:.1f}%",
                ])
            self._data_table(headers, rows, [18, 62, 38, 36, 36])

    def _time_trends(self):
        import matplotlib.pyplot as plt

        tt = self.metrics["time_trends"]
        if not tt:
            return

        self._section_header("Monthly Trends", "Performance trajectory over time")

        # Show last 12 months in detail table, full chart
        recent = tt[-12:] if len(tt) > 12 else tt
        headers = ["Month", "Won", "Lost", "Win Rate", "Won Value", "Avg Cycle"]
        rows = []
        for t in recent:
            rows.append([
                t["month"],
                str(t["won"]),
                str(t["lost"]),
                f"{t['win_rate']:.0f}%",
                f"${t['total_won_value']:,.0f}",
                f"{t['avg_cycle_days']:.0f}d" if t['avg_cycle_days'] > 0 else "-",
            ])
        self._data_table(headers, rows, [30, 20, 20, 28, 52, 40])

        # Multi-panel chart
        months = [t["month"] for t in tt]
        won_counts = [t["won"] for t in tt]
        lost_counts = [t["lost"] for t in tt]
        win_rates = [t["win_rate"] for t in tt]
        won_values = [t["total_won_value"] for t in tt]

        fig, axes = plt.subplots(3, 1, figsize=(9, 7))

        x = range(len(months))
        # Only show every Nth label for readability
        step = max(1, len(months) // 12)
        tick_positions = list(range(0, len(months), step))
        tick_labels = [months[i] for i in tick_positions]

        # Panel 1: Stacked volume
        axes[0].bar(x, won_counts, color=self.CH_SUCCESS, label="Won", edgecolor='white', linewidth=0.3)
        axes[0].bar(x, lost_counts, bottom=won_counts, color=self.CH_DANGER, label="Lost", edgecolor='white', linewidth=0.3)
        axes[0].set_xticks(tick_positions)
        axes[0].set_xticklabels(tick_labels, rotation=45, ha="right", fontsize=7)
        axes[0].set_title("Deal Volume by Month", pad=10)
        axes[0].legend(fontsize=8, ncol=2)
        axes[0].grid(axis='y', alpha=0.3)

        # Panel 2: Win rate with 3-month moving average
        axes[1].bar(x, win_rates, color=self.CH_ACCENT, alpha=0.3, label="Monthly", width=0.8)
        if len(win_rates) >= 3:
            ma = [mean(win_rates[max(0, i - 2):i + 1]) for i in range(len(win_rates))]
            axes[1].plot(list(x), ma, color=self.CH_ACCENT, linewidth=2.5, label="3-Month MA", marker='')
        axes[1].set_xticks(tick_positions)
        axes[1].set_xticklabels(tick_labels, rotation=45, ha="right", fontsize=7)
        axes[1].set_title("Win Rate Trend", pad=10)
        axes[1].set_ylabel("Win Rate (%)")
        axes[1].set_ylim(0, 100)
        axes[1].legend(fontsize=8)
        axes[1].grid(axis='y', alpha=0.3)

        # Panel 3: Revenue trend with moving average
        axes[2].bar(x, won_values, color=self.CH_SUCCESS, alpha=0.3, label="Monthly", width=0.8)
        if len(won_values) >= 3:
            rev_ma = [mean(won_values[max(0, i - 2):i + 1]) for i in range(len(won_values))]
            axes[2].plot(list(x), rev_ma, color=self.CH_SUCCESS, linewidth=2.5, label="3-Month MA")
        axes[2].set_xticks(tick_positions)
        axes[2].set_xticklabels(tick_labels, rotation=45, ha="right", fontsize=7)
        axes[2].set_title("Revenue Won by Month", pad=10)
        axes[2].yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x/1000:.0f}K" if x >= 1000 else f"${x:.0f}"))
        axes[2].legend(fontsize=8)
        axes[2].grid(axis='y', alpha=0.3)

        fig.tight_layout(pad=1.5)
        self._embed_chart(fig, width=185)

        # Trend insight
        if len(win_rates) >= 6:
            first_half = mean(win_rates[:len(win_rates) // 2])
            second_half = mean(win_rates[len(win_rates) // 2:])
            delta = second_half - first_half
            if abs(delta) > 5:
                direction = "improving" if delta > 0 else "declining"
                style = "success" if delta > 0 else "warning"
                self._insight_box(
                    f"Win rate is {direction}: first-half average was {first_half:.1f}%, second-half is {second_half:.1f}% ({delta:+.1f}pp). "
                    f"{'The trend suggests recent process improvements are taking hold.' if delta > 0 else 'Investigate whether this reflects market changes, team changes, or process drift.'}",
                    style
                )

    def _quarterly_trends(self):
        import matplotlib.pyplot as plt

        qt = self.metrics["quarterly_trends"]
        if len(qt) < 2:
            return

        self._section_header("Quarterly Performance", "Quarter-over-quarter comparison for strategic planning")

        headers = ["Quarter", "Deals", "Won", "Win Rate", "Revenue Won", "Avg Won"]
        rows = []
        for q in qt:
            rows.append([
                q["quarter"],
                str(q["total"]),
                str(q["won"]),
                f"{q['win_rate']:.1f}%",
                f"${q['total_won_value']:,.0f}",
                f"${q['avg_won_value']:,.0f}",
            ])
        self._data_table(headers, rows, [30, 22, 22, 28, 48, 40])

        fig, axes = plt.subplots(1, 2, figsize=(9, 3.5))

        quarters = [q["quarter"] for q in qt]
        x = range(len(quarters))

        # Revenue bars with QoQ growth
        rev = [q["total_won_value"] for q in qt]
        bars = axes[0].bar(x, rev, color=self.CH_ACCENT, width=0.6, edgecolor='white')
        axes[0].set_xticks(list(x))
        axes[0].set_xticklabels(quarters, rotation=45, ha="right", fontsize=8)
        axes[0].set_title("Quarterly Revenue", pad=10)
        axes[0].yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x/1000:.0f}K" if x >= 1000 else f"${x:.0f}"))
        axes[0].grid(axis='y', alpha=0.3)
        # QoQ growth labels
        for i in range(1, len(rev)):
            if rev[i - 1] > 0:
                growth = (rev[i] - rev[i - 1]) / rev[i - 1] * 100
                color = self.CH_SUCCESS if growth >= 0 else self.CH_DANGER
                axes[0].text(i, rev[i] + max(rev) * 0.02, f"{growth:+.0f}%",
                             ha='center', fontsize=7, fontweight='bold', color=color)

        # Win rate line
        win_rates = [q["win_rate"] for q in qt]
        axes[1].plot(list(x), win_rates, color=self.CH_ACCENT, linewidth=2.5, marker='o', markersize=6)
        axes[1].fill_between(list(x), win_rates, alpha=0.1, color=self.CH_ACCENT)
        axes[1].set_xticks(list(x))
        axes[1].set_xticklabels(quarters, rotation=45, ha="right", fontsize=8)
        axes[1].set_title("Quarterly Win Rate", pad=10)
        axes[1].set_ylabel("Win Rate (%)")
        axes[1].set_ylim(0, 100)
        axes[1].grid(axis='y', alpha=0.3)

        fig.tight_layout(pad=2)
        self._embed_chart(fig)

    def _rep_performance(self):
        import matplotlib.pyplot as plt

        all_reps = self.metrics["rep_performance"]

        # Filter: remove reps that look like deactivated IDs (no alpha chars in name)
        reps = [r for r in all_reps if any(c.isalpha() for c in r["owner_name"])]

        # Separate active reps (with wins) from zero-win reps for table vs chart
        reps_with_wins = [r for r in reps if r["won"] > 0]
        reps_no_wins = [r for r in reps if r["won"] == 0]

        self._section_header("Rep Performance", "Individual contributor analysis across key sales metrics")

        # Table shows all named reps
        headers = ["Rep", "Deals", "Won", "Win Rate", "Revenue Won", "Avg Deal", "Velocity"]
        rows = []
        for r in reps:
            rows.append([
                r["owner_name"][:22],
                str(r["total_deals"]),
                str(r["won"]),
                f"{r['win_rate']:.0f}%",
                f"${r['total_won_value']:,.0f}",
                f"${r['avg_won_value']:,.0f}",
                f"${r['avg_velocity']:,.0f}/d" if r['avg_velocity'] > 0 else "-",
            ])

        # Note about filtered reps
        n_filtered = len(all_reps) - len(reps)
        if n_filtered > 0:
            self._body_text(f"Note: {n_filtered} deactivated user(s) excluded from this view.")

        self._data_table(headers, rows, [40, 18, 18, 25, 35, 28, 26], highlight_col=4)

        # Charts only show reps with at least 1 win
        chart_reps = reps_with_wins
        if len(chart_reps) >= 2:
            fig, axes = plt.subplots(1, 3, figsize=(9, max(3, len(chart_reps) * 0.45)))
            names = [r["owner_name"][:18] for r in chart_reps]

            # Revenue
            rev = [r["total_won_value"] for r in chart_reps]
            colors_rev = [self.CH_ACCENT if v == max(rev) else self.CH_ACCENT_LIGHT for v in rev]
            axes[0].barh(names, rev, color=colors_rev, edgecolor='white', linewidth=0.5)
            axes[0].set_title("Revenue Won", pad=10)
            axes[0].xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x/1000:.0f}K" if x >= 1000 else f"${x:.0f}"))
            axes[0].invert_yaxis()
            axes[0].grid(axis='x', alpha=0.3)

            # Win rate
            rates = [r["win_rate"] for r in chart_reps]
            colors_wr = [self.CH_SUCCESS if v >= 50 else (self.CH_WARNING if v >= 25 else self.CH_DANGER) for v in rates]
            axes[1].barh(names, rates, color=colors_wr, edgecolor='white', linewidth=0.5)
            axes[1].set_title("Win Rate (%)", pad=10)
            axes[1].set_xlim(0, 100)
            axes[1].invert_yaxis()
            axes[1].grid(axis='x', alpha=0.3)

            # Velocity
            vel = [r["avg_velocity"] for r in chart_reps]
            colors_vel = [self.CH_PURPLE if v == max(vel) else self.CH_ACCENT_LIGHT for v in vel]
            axes[2].barh(names, vel, color=colors_vel, edgecolor='white', linewidth=0.5)
            axes[2].set_title("Velocity ($/day)", pad=10)
            axes[2].xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
            axes[2].invert_yaxis()
            axes[2].grid(axis='x', alpha=0.3)

            fig.tight_layout(pad=1.5)
            self._embed_chart(fig)

            # Insight: identify high-velocity vs high-volume reps
            if len(chart_reps) >= 3:
                top_rev = max(chart_reps, key=lambda r: r["total_won_value"])
                top_wr = max(chart_reps, key=lambda r: r["win_rate"] if r["total_deals"] >= 5 else 0)
                top_vel = max(chart_reps, key=lambda r: r["avg_velocity"])
                insights = []
                if top_rev["owner_name"] != top_vel["owner_name"]:
                    insights.append(
                        f"{top_rev['owner_name']} leads in revenue (${top_rev['total_won_value']:,.0f}) while "
                        f"{top_vel['owner_name']} leads in velocity (${top_vel['avg_velocity']:,.0f}/day). "
                        f"Revenue leaders drive volume; velocity leaders maximize efficiency."
                    )
                if insights:
                    self._insight_box(insights[0])

    def _source_analysis(self):
        import matplotlib.pyplot as plt

        sources = self.metrics["source_analysis"]
        self._section_header("Lead Source Analysis", "Which channels deliver the best deals?")

        headers = ["Source", "Deals", "Won", "Win Rate", "Revenue Won", "Avg Deal", "Avg Cycle"]
        rows = []
        for s in sources:
            rows.append([
                s["source"][:22],
                str(s["total_deals"]),
                str(s["won"]),
                f"{s['win_rate']:.0f}%",
                f"${s['total_won_value']:,.0f}",
                f"${s['avg_won_value']:,.0f}",
                f"{s['avg_cycle_days']:.0f}d" if s['avg_cycle_days'] > 0 else "-",
            ])
        self._data_table(headers, rows, [38, 18, 18, 24, 30, 30, 22])

        # Only chart sources with meaningful volume (3+ deals)
        chart_sources = [s for s in sources if s["total_deals"] >= 3]
        if not chart_sources:
            chart_sources = sources[:5]

        if len(chart_sources) >= 2:
            import numpy as np

            fig, axes = plt.subplots(1, 3, figsize=(9, max(3, len(chart_sources) * 0.45)))
            names = [s["source"][:18] for s in chart_sources]

            # Volume
            vols = [s["total_deals"] for s in chart_sources]
            axes[0].barh(names, vols, color=self.CH_ACCENT, edgecolor='white', linewidth=0.5)
            axes[0].set_title("Deal Volume", pad=10)
            axes[0].invert_yaxis()
            axes[0].grid(axis='x', alpha=0.3)

            # Win rate
            wrs = [s["win_rate"] for s in chart_sources]
            wr_colors = [self.CH_SUCCESS if v >= 30 else (self.CH_WARNING if v >= 15 else self.CH_DANGER) for v in wrs]
            axes[1].barh(names, wrs, color=wr_colors, edgecolor='white', linewidth=0.5)
            axes[1].set_title("Win Rate (%)", pad=10)
            axes[1].set_xlim(0, max(100, max(wrs) + 10))
            axes[1].invert_yaxis()
            axes[1].grid(axis='x', alpha=0.3)

            # Revenue
            revs = [s["total_won_value"] for s in chart_sources]
            axes[2].barh(names, revs, color=self.CH_SUCCESS, edgecolor='white', linewidth=0.5)
            axes[2].set_title("Revenue Won", pad=10)
            axes[2].xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x/1000:.0f}K" if x >= 1000 else f"${x:.0f}"))
            axes[2].invert_yaxis()
            axes[2].grid(axis='x', alpha=0.3)

            fig.tight_layout(pad=1.5)
            self._embed_chart(fig)

            # Insight
            if len(sources) >= 2:
                best_wr = max((s for s in sources if s["total_deals"] >= 3), key=lambda s: s["win_rate"], default=None)
                best_rev = max(sources, key=lambda s: s["total_won_value"])
                if best_wr and best_rev and best_wr["source"] != best_rev["source"]:
                    self._insight_box(
                        f"'{best_wr['source']}' has the highest win rate ({best_wr['win_rate']:.0f}%) while "
                        f"'{best_rev['source']}' generates the most revenue (${best_rev['total_won_value']:,.0f}). "
                        f"Increasing volume from high-win-rate sources could unlock revenue growth.",
                        "info"
                    )

    def _loss_reasons(self):
        import matplotlib.pyplot as plt

        lr = self.metrics["loss_reasons"]
        self._section_header("Loss Analysis", "Understanding why deals don't close")

        reasons = lr["reasons"]
        if not reasons:
            self._insight_box(
                "No loss reason data available. Adding a 'Closed Lost Reason' property to your deal pipeline "
                "would unlock critical insights into why deals are falling through.",
                "warning"
            )
            return

        self._body_text(f"Data source: {lr['property']}")

        headers = ["Reason", "Count", "% of Losses"]
        total = sum(reasons.values())
        rows = []
        for reason, count in reasons.items():
            rows.append([
                reason[:45],
                str(count),
                f"{count / total * 100:.1f}%",
            ])
        self._data_table(headers, rows, [90, 40, 60])

        # Horizontal bar chart (sorted)
        fig, axes = plt.subplots(1, 2, figsize=(9, max(3, len(reasons) * 0.4)))

        sorted_reasons = sorted(reasons.items(), key=lambda x: x[1], reverse=True)
        labels = [r[0][:30] for r in sorted_reasons]
        values = [r[1] for r in sorted_reasons]

        # Gradient colors from dark red to light
        n = len(values)
        colors_bar = []
        for i in range(n):
            t = i / max(1, n - 1)
            r = int(220 - t * 80)
            g = int(53 + t * 100)
            b = int(69 + t * 100)
            colors_bar.append(f"#{r:02x}{g:02x}{b:02x}")

        axes[0].barh(labels[::-1], values[::-1], color=colors_bar[::-1], edgecolor='white', linewidth=0.5)
        axes[0].set_title("Loss Reasons (Count)", pad=10)
        axes[0].set_xlabel("Number of Deals")
        axes[0].grid(axis='x', alpha=0.3)

        # Pie/donut chart
        top_reasons = sorted_reasons[:6]
        if len(sorted_reasons) > 6:
            other_count = sum(v for _, v in sorted_reasons[6:])
            top_reasons.append(("Other", other_count))

        pie_labels = [r[0][:20] for r in top_reasons]
        pie_values = [r[1] for r in top_reasons]
        pie_colors = [self.CH_DANGER, self.CH_WARNING, self.CH_ACCENT, self.CH_PURPLE,
                      self.CH_SUCCESS, self.CH_ACCENT_LIGHT, self.CH_MID][:len(pie_values)]

        wedges, texts, autotexts = axes[1].pie(
            pie_values, labels=pie_labels, autopct='%1.0f%%', startangle=90,
            colors=pie_colors, wedgeprops=dict(width=0.45, edgecolor='white', linewidth=2),
            pctdistance=0.75, textprops={'fontsize': 7}
        )
        for t in autotexts:
            t.set_fontsize(8)
            t.set_fontweight('bold')
        axes[1].set_title("Loss Reason Distribution", pad=10)

        fig.tight_layout(pad=2)
        self._embed_chart(fig)

        # Loss reasons by tier
        if self.metrics.get("loss_reasons_by_tier"):
            lbt = self.metrics["loss_reasons_by_tier"]
            non_empty = {t: r for t, r in lbt.items() if r}
            if non_empty:
                self._sub_header("Loss Patterns by Deal Size")
                for tier_name in ["Small", "Mid-Market", "Large"]:
                    if tier_name in non_empty and non_empty[tier_name]:
                        top_reason = max(non_empty[tier_name].items(), key=lambda x: x[1])
                        tier_total = sum(non_empty[tier_name].values())
                        self._body_text(
                            f"{tier_name}: Top loss reason is \"{top_reason[0]}\" "
                            f"({top_reason[1]}/{tier_total} = {top_reason[1]/tier_total*100:.0f}% of {tier_name.lower()} losses)"
                        )

        # Top insight
        if sorted_reasons:
            top = sorted_reasons[0]
            pct = top[1] / total * 100
            if pct > 30:
                self._insight_box(
                    f"\"{top[0]}\" accounts for {pct:.0f}% of all losses ({top[1]} deals). "
                    f"This is the single biggest controllable factor. Addressing it could meaningfully move the win rate needle.",
                    "danger"
                )

    def _stage_dropoff(self):
        import matplotlib.pyplot as plt

        sd = self.metrics["stage_dropoff"]
        self._section_header("Pipeline Stage Drop-off", "Where in the pipeline are deals dying?")

        headers = ["Stage", "Lost Deals", "% of Losses"]
        total = sum(sd.values())
        rows = []
        for stage, count in sd.items():
            rows.append([
                stage[:35],
                str(count),
                f"{count / total * 100:.1f}%",
            ])
        self._data_table(headers, rows, [80, 50, 60])

        if sd:
            fig, ax = plt.subplots(figsize=(8, max(3, len(sd) * 0.5)))

            stages = list(sd.keys())
            counts = list(sd.values())

            # Color gradient: more losses = more red
            max_count = max(counts)
            colors_bar = []
            for c in counts:
                intensity = c / max_count
                r = int(220 * intensity + 180 * (1 - intensity))
                g = int(53 * intensity + 180 * (1 - intensity))
                b = int(69 * intensity + 180 * (1 - intensity))
                colors_bar.append(f"#{r:02x}{g:02x}{b:02x}")

            bars = ax.barh(stages[::-1], counts[::-1], color=colors_bar[::-1],
                           edgecolor='white', linewidth=0.5, height=0.6)
            ax.set_xlabel("Deals Lost")
            ax.set_title("Deals Lost by Pipeline Stage", pad=12)
            ax.grid(axis='x', alpha=0.3)

            # Value labels
            for bar, count in zip(bars, counts[::-1]):
                ax.text(bar.get_width() + max_count * 0.02, bar.get_y() + bar.get_height() / 2,
                        str(count), va='center', fontsize=9, fontweight='bold', color=self.CH_CHARCOAL)

            fig.tight_layout()
            self._embed_chart(fig)

            # Insight — skip if the top stage is a terminal stage (closed lost / disqualified)
            top_stage = max(sd.items(), key=lambda x: x[1])
            top_pct = top_stage[1] / total * 100
            stage_name_lower = top_stage[0].lower()
            is_terminal = any(kw in stage_name_lower for kw in ["closed", "lost", "disqualif"])
            if top_pct > 40 and not is_terminal:
                self._insight_box(
                    f"{top_pct:.0f}% of losses occur at the '{top_stage[0]}' stage. "
                    f"This is your critical pipeline bottleneck. Review what's happening at this stage - "
                    f"are deals stalling? Are they being qualified too late? Is there a handoff issue?",
                    "danger"
                )
            elif len(sd) >= 2:
                # Find the top non-terminal stage for a more useful insight
                non_terminal = [(s, c) for s, c in sd.items()
                                if not any(kw in s.lower() for kw in ["closed", "lost", "disqualif"])]
                if non_terminal:
                    nt_stage, nt_count = max(non_terminal, key=lambda x: x[1])
                    nt_pct = nt_count / total * 100
                    if nt_pct > 5:
                        self._insight_box(
                            f"Outside of terminal stages, '{nt_stage}' sees the most drop-off with {nt_count} lost deals ({nt_pct:.0f}% of losses). "
                            f"Investigate deal behavior at this stage for potential process improvements.",
                            "warning"
                        )


# ---------------------------------------------------------------------------
# Analyze command
# ---------------------------------------------------------------------------

def cmd_fetch(args):
    """Fetch deals from HubSpot and save raw data to JSON."""
    if not TOKEN:
        print("ERROR: HUBSPOT_ACCESS_TOKEN not found in .env", file=sys.stderr)
        sys.exit(1)

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    print("Fetching pipeline info...", file=sys.stderr)
    pipelines = api_request("GET", "/crm/v3/pipelines/deals").get("results", [])
    pipeline_id = config.get("pipeline_id", "default")

    target_pipeline = None
    for pl in pipelines:
        if pl["id"] == pipeline_id or (pipeline_id == "default" and pl.get("displayOrder", 99) == 0):
            target_pipeline = pl
            break
    if not target_pipeline and pipelines:
        target_pipeline = pipelines[0]

    pipeline_stages = []
    if target_pipeline:
        for s in target_pipeline.get("stages", []):
            pipeline_stages.append({
                "id": s["id"],
                "label": s["label"],
                "displayOrder": s.get("displayOrder", 0),
            })
        pipeline_stages.sort(key=lambda x: x["displayOrder"])

    print("Fetching owners...", file=sys.stderr)
    owners_raw = paginate_list("/crm/v3/owners/")
    owners_map = {}
    for o in owners_raw:
        owners_map[o["id"]] = f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()

    deals = fetch_deals(config, pipeline_stages)

    if not deals:
        print("ERROR: No deals found matching the criteria.", file=sys.stderr)
        sys.exit(1)

    # Save raw data + context
    output = {
        "deals": deals,
        "pipeline_stages": pipeline_stages,
        "owners_map": owners_map,
        "config": config,
    }
    output_path = SCRIPT_DIR / "deals.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    # Print summary for Claude to read
    amounts = [_safe_float(d.get("properties", {}).get("amount")) for d in deals]
    amounts = [a for a in amounts if a is not None]
    won_stages = set(config.get("closed_won_stages", []))
    lost_stages = set(config.get("closed_lost_stages", []))
    won_count = sum(1 for d in deals if d.get("properties", {}).get("dealstage") in won_stages)
    lost_count = sum(1 for d in deals if d.get("properties", {}).get("dealstage") in lost_stages)

    summary = {
        "deals_saved_to": str(output_path),
        "total_deals": len(deals),
        "won_count": won_count,
        "lost_count": lost_count,
        "value_range": {"min": min(amounts) if amounts else 0, "max": max(amounts) if amounts else 0},
        "total_value": sum(amounts),
        "date_range": config.get("time_period", {}),
        "unique_owners": len(set(d.get("properties", {}).get("hubspot_owner_id", "") for d in deals)),
        "unique_sources": len(set(d.get("properties", {}).get("hs_analytics_source", "") for d in deals)),
    }
    print(json.dumps(summary, indent=2))


def cmd_metrics(args):
    """Compute metrics from fetched deal data."""
    data_path = Path(args.data)
    if not data_path.exists():
        print(f"ERROR: Data file not found: {data_path}", file=sys.stderr)
        sys.exit(1)

    with open(data_path) as f:
        data = json.load(f)

    deals = data["deals"]
    pipeline_stages = data["pipeline_stages"]
    owners_map = data["owners_map"]
    config = data["config"]

    print("Computing metrics...", file=sys.stderr)
    metrics = compute_metrics(deals, config, pipeline_stages, owners_map)

    # Save full metrics
    output_path = SCRIPT_DIR / "analysis.json"
    with open(output_path, "w") as f:
        json.dump(metrics, f, indent=2, default=str)

    print(f"Metrics saved to: {output_path}", file=sys.stderr)
    print(json.dumps(metrics, indent=2, default=str))


def cmd_report(args):
    """Generate an HTML report from analysis metrics."""
    data_path = Path(args.data)
    if not data_path.exists():
        print(f"ERROR: Data file not found: {data_path}", file=sys.stderr)
        sys.exit(1)

    with open(data_path) as f:
        metrics = json.load(f)

    sections = args.sections.split(",") if args.sections else None
    findings = args.findings if args.findings else None

    from html_report import HTMLReportBuilder
    builder = HTMLReportBuilder(metrics, sections=sections, findings=findings)
    output_name = f"report_{datetime.now().strftime('%Y-%m-%d')}.html"
    output_path = SCRIPT_DIR / output_name
    builder.build(str(output_path))

    print(f"Report generated: {output_path}", file=sys.stderr)
    print(json.dumps({"report_path": str(output_path)}, indent=2))


def cmd_analyze(args):
    """Run deal analysis and generate PDF report."""
    if not TOKEN:
        print("ERROR: HUBSPOT_ACCESS_TOKEN not found in .env", file=sys.stderr)
        sys.exit(1)

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    print("Fetching pipeline info...", file=sys.stderr)
    pipelines = api_request("GET", "/crm/v3/pipelines/deals").get("results", [])
    pipeline_id = config.get("pipeline_id", "default")

    target_pipeline = None
    for pl in pipelines:
        if pl["id"] == pipeline_id or (pipeline_id == "default" and pl.get("displayOrder", 99) == 0):
            target_pipeline = pl
            break
    if not target_pipeline and pipelines:
        target_pipeline = pipelines[0]

    pipeline_stages = []
    if target_pipeline:
        for s in target_pipeline.get("stages", []):
            pipeline_stages.append({
                "id": s["id"],
                "label": s["label"],
                "displayOrder": s.get("displayOrder", 0),
            })
        pipeline_stages.sort(key=lambda x: x["displayOrder"])

    print("Fetching owners...", file=sys.stderr)
    owners_raw = paginate_list("/crm/v3/owners/")
    owners_map = {}
    for o in owners_raw:
        owners_map[o["id"]] = f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()

    deals = fetch_deals(config, pipeline_stages)

    if not deals:
        print("ERROR: No deals found matching the criteria.", file=sys.stderr)
        sys.exit(1)

    print("Computing metrics...", file=sys.stderr)
    metrics = compute_metrics(deals, config, pipeline_stages, owners_map)

    print("Generating PDF report...", file=sys.stderr)
    report = ReportBuilder(metrics, config, pipeline_stages, owners_map)
    output_name = f"report_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    output_path = SCRIPT_DIR / output_name
    report.build(output_path)

    print(f"\nReport generated: {output_path}", file=sys.stderr)
    summary = {
        "report_path": str(output_path),
        "executive_summary": metrics["executive_summary"],
        "sections_included": list(metrics.keys()),
        "deal_count": len(deals),
    }
    print(json.dumps(summary, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Deal Doctor — HubSpot Pipeline Diagnosis Tool")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser("schema", help="Fetch and display deal schema")

    fetch_parser = subparsers.add_parser("fetch", help="Fetch deals and save raw data")
    fetch_parser.add_argument("--config", required=True, help="Path to analysis config JSON")

    metrics_parser = subparsers.add_parser("metrics", help="Compute metrics from fetched data")
    metrics_parser.add_argument("--data", required=True, help="Path to deals.json from fetch command")

    report_parser = subparsers.add_parser("report", help="Generate HTML report from metrics")
    report_parser.add_argument("--data", required=True, help="Path to analysis.json from metrics command")
    report_parser.add_argument("--sections", default=None, help="Comma-separated section IDs to include")
    report_parser.add_argument("--findings", default=None, help="JSON string of custom findings to embed")

    analyze_parser = subparsers.add_parser("analyze", help="Run full analysis and generate PDF report")
    analyze_parser.add_argument("--config", required=True, help="Path to analysis config JSON")

    args = parser.parse_args()

    if args.command == "schema":
        cmd_schema(args)
    elif args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "metrics":
        cmd_metrics(args)
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "analyze":
        cmd_analyze(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
