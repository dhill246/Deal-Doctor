"""
Deal Doctor — Interactive HTML Report Builder.

Generates a self-contained HTML file with Plotly.js charts, glassmorphism design,
dark/light mode, deal drill-down, and what-if calculator.
"""

import json
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


SCRIPT_DIR = Path(__file__).parent


class HTMLReportBuilder:
    """Generates an interactive HTML report from deal analysis metrics."""

    def __init__(self, metrics, sections=None, findings=None):
        self.metrics = metrics
        self.requested_sections = sections
        self.findings = findings
        self.available_sections = self._determine_available_sections()

    def _determine_available_sections(self):
        """Determine which sections have enough data to render."""
        m = self.metrics
        available = ["executive_summary"]

        es = m.get("executive_summary", {})
        if es.get("total_deals", 0) > 0:
            available.append("win_loss")

        dist = m.get("deal_size_distribution")
        if dist and dist.get("count", 0) >= 5:
            available.append("deal_size")

        tier = m.get("win_rate_by_tier")
        if tier and tier.get("tiers"):
            has_enough = all(t.get("total", 0) >= 3 for t in tier["tiers"].values())
            if has_enough:
                available.append("tier_analysis")

        sc = m.get("sales_cycle", {})
        if sc.get("won_cycles") or sc.get("lost_cycles"):
            available.append("sales_cycle")

        vel = m.get("deal_velocity", {})
        if len(vel.get("won", [])) >= 5:
            available.append("deal_velocity")

        conc = m.get("revenue_concentration")
        if conc and conc.get("pareto_data"):
            available.append("revenue_concentration")

        if len(m.get("time_trends", [])) >= 3:
            available.append("time_trends")

        if len(m.get("quarterly_trends", [])) >= 2:
            available.append("quarterly_trends")

        reps = m.get("rep_performance", [])
        named_reps = [r for r in reps if r.get("owner_name") not in ("Unassigned", "", None)]
        if len(named_reps) >= 2:
            available.append("rep_performance")

        sources = m.get("source_analysis", [])
        if len(sources) >= 2:
            available.append("source_analysis")

        lr = m.get("loss_reasons", {})
        reasons = lr.get("reasons", {})
        real_reasons = {k: v for k, v in reasons.items() if k != "Not specified"}
        if real_reasons:
            available.append("loss_reasons")

        if m.get("stage_dropoff"):
            available.append("stage_dropoff")

        scatter = m.get("cycle_vs_size", [])
        if len(scatter) >= 10:
            available.append("cycle_vs_size")

        return available

    def _get_sections_to_render(self):
        """Return the final list of sections to render."""
        if self.requested_sections:
            return [s for s in self.requested_sections if s in self.available_sections]
        return self.available_sections

    def _prepare_chart_configs(self):
        """Build Plotly chart config dicts for client-side rendering."""
        m = self.metrics
        configs = {}

        # Win/Loss donut
        es = m.get("executive_summary", {})
        won = es.get("won_count", 0)
        lost = es.get("lost_count", 0)
        if won + lost > 0:
            configs["win_loss_donut"] = {
                "data": [{
                    "values": [won, lost],
                    "labels": ["Won", "Lost"],
                    "type": "pie",
                    "hole": 0.55,
                    "marker": {"colors": ["#10a37f", "#dc3545"]},
                    "textinfo": "percent+label",
                    "textfont": {"size": 14, "color": "#ffffff"},
                    "hovertemplate": "%{label}: %{value} deals (%{percent})<extra></extra>",
                }],
                "layout": {
                    "showlegend": False,
                    "margin": {"t": 20, "b": 20, "l": 20, "r": 20},
                    "paper_bgcolor": "transparent",
                    "plot_bgcolor": "transparent",
                    "annotations": [{
                        "text": f"<b>{won + lost}</b><br>deals",
                        "showarrow": False,
                        "font": {"size": 20},
                    }],
                },
            }

        # Win/Loss value bars
        wl = m.get("win_loss", {})
        if wl:
            configs["win_loss_value"] = {
                "data": [{
                    "x": ["Won", "Lost"],
                    "y": [wl.get("won", {}).get("total_value", 0), wl.get("lost", {}).get("total_value", 0)],
                    "type": "bar",
                    "marker": {"color": ["#10a37f", "#dc3545"], "cornerradius": 6},
                    "hovertemplate": "%{x}: $%{y:,.0f}<extra></extra>",
                }],
                "layout": {
                    "showlegend": False,
                    "margin": {"t": 20, "b": 40, "l": 60, "r": 20},
                    "paper_bgcolor": "transparent",
                    "plot_bgcolor": "transparent",
                    "yaxis": {"gridcolor": "rgba(128,128,128,0.15)", "tickprefix": "$", "tickformat": ",.0f"},
                    "xaxis": {"fixedrange": True},
                },
            }

        # Deal size histogram
        dist = m.get("deal_size_distribution")
        if dist and dist.get("won_values") and dist.get("lost_values"):
            configs["deal_size_hist"] = {
                "data": [
                    {"x": dist["won_values"], "type": "histogram", "name": "Won",
                     "marker": {"color": "rgba(16,163,127,0.7)"}, "nbinsx": 15},
                    {"x": dist["lost_values"], "type": "histogram", "name": "Lost",
                     "marker": {"color": "rgba(220,53,69,0.7)"}, "nbinsx": 15},
                ],
                "layout": {
                    "barmode": "overlay",
                    "showlegend": True,
                    "legend": {"x": 0.75, "y": 0.95},
                    "margin": {"t": 20, "b": 40, "l": 60, "r": 20},
                    "paper_bgcolor": "transparent",
                    "plot_bgcolor": "transparent",
                    "xaxis": {"title": "Deal Size", "tickprefix": "$", "tickformat": ",.0f"},
                    "yaxis": {"title": "Count", "gridcolor": "rgba(128,128,128,0.15)"},
                },
            }

        # Tier analysis
        tier = m.get("win_rate_by_tier")
        if tier and tier.get("tiers"):
            tiers = tier["tiers"]
            tier_names = ["Small", "Mid-Market", "Large"]
            tier_names = [t for t in tier_names if t in tiers]
            configs["tier_win_rate"] = {
                "data": [{
                    "x": tier_names,
                    "y": [tiers[t]["win_rate"] for t in tier_names],
                    "type": "bar",
                    "marker": {"color": ["#0b99d9", "#6f44c4", "#f5a623"], "cornerradius": 6},
                    "hovertemplate": "%{x}: %{y:.1f}%<extra></extra>",
                }],
                "layout": {
                    "showlegend": False,
                    "margin": {"t": 20, "b": 40, "l": 50, "r": 20},
                    "paper_bgcolor": "transparent",
                    "plot_bgcolor": "transparent",
                    "yaxis": {"title": "Win Rate %", "gridcolor": "rgba(128,128,128,0.15)", "range": [0, 100]},
                },
            }

        # Sales cycle box plot
        sc = m.get("sales_cycle", {})
        won_cycles = sc.get("won_cycles", [])
        lost_cycles = sc.get("lost_cycles", [])
        if won_cycles or lost_cycles:
            cycle_data = []
            if won_cycles:
                cycle_data.append({"y": won_cycles, "type": "box", "name": "Won",
                                   "marker": {"color": "#10a37f"}, "boxmean": True})
            if lost_cycles:
                cycle_data.append({"y": lost_cycles, "type": "box", "name": "Lost",
                                   "marker": {"color": "#dc3545"}, "boxmean": True})
            configs["sales_cycle_box"] = {
                "data": cycle_data,
                "layout": {
                    "showlegend": False,
                    "margin": {"t": 20, "b": 40, "l": 50, "r": 20},
                    "paper_bgcolor": "transparent",
                    "plot_bgcolor": "transparent",
                    "yaxis": {"title": "Days to Close", "gridcolor": "rgba(128,128,128,0.15)"},
                },
            }

        # Cycle vs Size scatter
        scatter = m.get("cycle_vs_size", [])
        if len(scatter) >= 10:
            won_pts = [p for p in scatter if p["is_won"]]
            lost_pts = [p for p in scatter if not p["is_won"]]
            scatter_data = []
            if won_pts:
                scatter_data.append({
                    "x": [p["cycle_days"] for p in won_pts],
                    "y": [p["amount"] for p in won_pts],
                    "mode": "markers",
                    "type": "scatter",
                    "name": "Won",
                    "marker": {"color": "#10a37f", "size": 10, "opacity": 0.7},
                    "hovertemplate": "%{y:$,.0f} in %{x} days<extra>Won</extra>",
                })
            if lost_pts:
                scatter_data.append({
                    "x": [p["cycle_days"] for p in lost_pts],
                    "y": [p["amount"] for p in lost_pts],
                    "mode": "markers",
                    "type": "scatter",
                    "name": "Lost",
                    "marker": {"color": "#dc3545", "size": 10, "opacity": 0.7},
                    "hovertemplate": "%{y:$,.0f} in %{x} days<extra>Lost</extra>",
                })
            configs["cycle_vs_size"] = {
                "data": scatter_data,
                "layout": {
                    "showlegend": True,
                    "legend": {"x": 0.75, "y": 0.95},
                    "margin": {"t": 20, "b": 50, "l": 70, "r": 20},
                    "paper_bgcolor": "transparent",
                    "plot_bgcolor": "transparent",
                    "xaxis": {"title": "Days to Close", "gridcolor": "rgba(128,128,128,0.15)"},
                    "yaxis": {"title": "Deal Size", "tickprefix": "$", "tickformat": ",.0f",
                              "gridcolor": "rgba(128,128,128,0.15)"},
                },
            }

        # Revenue concentration (Lorenz curve)
        conc = m.get("revenue_concentration")
        if conc and conc.get("pareto_data"):
            pd = conc["pareto_data"]
            configs["lorenz_curve"] = {
                "data": [
                    {"x": [0] + [p["deal_pct"] for p in pd],
                     "y": [0] + [p["cumulative_pct"] for p in pd],
                     "type": "scatter", "mode": "lines",
                     "name": "Your Portfolio",
                     "line": {"color": "#0b99d9", "width": 3},
                     "fill": "tozeroy",
                     "fillcolor": "rgba(11,153,217,0.1)"},
                    {"x": [0, 100], "y": [0, 100],
                     "type": "scatter", "mode": "lines",
                     "name": "Perfect Equality",
                     "line": {"color": "rgba(128,128,128,0.5)", "dash": "dash", "width": 2}},
                ],
                "layout": {
                    "showlegend": True,
                    "legend": {"x": 0.05, "y": 0.95},
                    "margin": {"t": 20, "b": 50, "l": 50, "r": 20},
                    "paper_bgcolor": "transparent",
                    "plot_bgcolor": "transparent",
                    "xaxis": {"title": "% of Deals", "range": [0, 100],
                              "gridcolor": "rgba(128,128,128,0.15)"},
                    "yaxis": {"title": "% of Revenue", "range": [0, 100],
                              "gridcolor": "rgba(128,128,128,0.15)"},
                },
            }

        # Time trends
        trends = m.get("time_trends", [])
        if len(trends) >= 3:
            months = [t["month"] for t in trends]
            configs["time_trends"] = {
                "data": [
                    {"x": months, "y": [t["won"] for t in trends],
                     "type": "bar", "name": "Won", "marker": {"color": "#10a37f"}},
                    {"x": months, "y": [t["lost"] for t in trends],
                     "type": "bar", "name": "Lost", "marker": {"color": "#dc3545"}},
                    {"x": months, "y": [t["win_rate"] for t in trends],
                     "type": "scatter", "mode": "lines+markers", "name": "Win Rate %",
                     "yaxis": "y2", "line": {"color": "#0b99d9", "width": 2}},
                ],
                "layout": {
                    "barmode": "stack",
                    "showlegend": True,
                    "legend": {"x": 0.01, "y": 1.15, "orientation": "h"},
                    "margin": {"t": 30, "b": 50, "l": 50, "r": 50},
                    "paper_bgcolor": "transparent",
                    "plot_bgcolor": "transparent",
                    "xaxis": {"gridcolor": "rgba(128,128,128,0.15)"},
                    "yaxis": {"title": "Deal Count", "gridcolor": "rgba(128,128,128,0.15)"},
                    "yaxis2": {"title": "Win Rate %", "overlaying": "y", "side": "right",
                               "range": [0, 100], "gridcolor": "rgba(128,128,128,0.15)"},
                },
            }

        # Rep performance
        reps = m.get("rep_performance", [])
        named_reps = [r for r in reps if r.get("owner_name") not in ("Unassigned", "", None)]
        if len(named_reps) >= 2:
            rep_names = [r["owner_name"] for r in named_reps]
            configs["rep_win_rate"] = {
                "data": [{
                    "y": rep_names,
                    "x": [r["win_rate"] for r in named_reps],
                    "type": "bar",
                    "orientation": "h",
                    "marker": {"color": [
                        "#10a37f" if r["win_rate"] >= 50 else "#f5a623" if r["win_rate"] >= 25 else "#dc3545"
                        for r in named_reps
                    ], "cornerradius": 4},
                    "hovertemplate": "%{y}: %{x:.1f}%<extra></extra>",
                }],
                "layout": {
                    "showlegend": False,
                    "margin": {"t": 20, "b": 40, "l": 120, "r": 20},
                    "paper_bgcolor": "transparent",
                    "plot_bgcolor": "transparent",
                    "xaxis": {"title": "Win Rate %", "range": [0, 100],
                              "gridcolor": "rgba(128,128,128,0.15)"},
                },
            }
            configs["rep_revenue"] = {
                "data": [{
                    "y": rep_names,
                    "x": [r["total_won_value"] for r in named_reps],
                    "type": "bar",
                    "orientation": "h",
                    "marker": {"color": "#0b99d9", "cornerradius": 4},
                    "hovertemplate": "%{y}: $%{x:,.0f}<extra></extra>",
                }],
                "layout": {
                    "showlegend": False,
                    "margin": {"t": 20, "b": 40, "l": 120, "r": 20},
                    "paper_bgcolor": "transparent",
                    "plot_bgcolor": "transparent",
                    "xaxis": {"title": "Revenue Won", "tickprefix": "$", "tickformat": ",.0f",
                              "gridcolor": "rgba(128,128,128,0.15)"},
                },
            }

        # Source analysis
        sources = m.get("source_analysis", [])
        if len(sources) >= 2:
            src_names = [s["source"] for s in sources[:8]]
            configs["source_deals"] = {
                "data": [{
                    "y": src_names,
                    "x": [s["total_deals"] for s in sources[:8]],
                    "type": "bar",
                    "orientation": "h",
                    "marker": {"color": "#0b99d9", "cornerradius": 4},
                    "hovertemplate": "%{y}: %{x} deals<extra></extra>",
                }],
                "layout": {
                    "showlegend": False,
                    "margin": {"t": 20, "b": 40, "l": 120, "r": 20},
                    "paper_bgcolor": "transparent",
                    "plot_bgcolor": "transparent",
                    "xaxis": {"title": "Deals", "gridcolor": "rgba(128,128,128,0.15)"},
                },
            }

        # Loss reasons
        lr = m.get("loss_reasons", {})
        reasons = lr.get("reasons", {})
        real_reasons = {k: v for k, v in reasons.items() if k != "Not specified"}
        if real_reasons:
            sorted_reasons = sorted(real_reasons.items(), key=lambda x: x[1], reverse=True)
            configs["loss_reasons"] = {
                "data": [{
                    "y": [r[0] for r in sorted_reasons],
                    "x": [r[1] for r in sorted_reasons],
                    "type": "bar",
                    "orientation": "h",
                    "marker": {"color": "#dc3545", "cornerradius": 4},
                    "hovertemplate": "%{y}: %{x} deals<extra></extra>",
                }],
                "layout": {
                    "showlegend": False,
                    "margin": {"t": 20, "b": 40, "l": 180, "r": 20},
                    "paper_bgcolor": "transparent",
                    "plot_bgcolor": "transparent",
                    "xaxis": {"title": "Lost Deals", "gridcolor": "rgba(128,128,128,0.15)"},
                },
            }

        # Stage drop-off
        dropoff = m.get("stage_dropoff", {})
        if dropoff:
            stages = list(dropoff.keys())
            counts = list(dropoff.values())
            configs["stage_dropoff"] = {
                "data": [{
                    "y": stages,
                    "x": counts,
                    "type": "bar",
                    "orientation": "h",
                    "marker": {"color": "#f5a623", "cornerradius": 4},
                    "hovertemplate": "%{y}: %{x} losses<extra></extra>",
                }],
                "layout": {
                    "showlegend": False,
                    "margin": {"t": 20, "b": 40, "l": 140, "r": 20},
                    "paper_bgcolor": "transparent",
                    "plot_bgcolor": "transparent",
                    "xaxis": {"title": "Deals Lost", "gridcolor": "rgba(128,128,128,0.15)"},
                },
            }

        return configs

    def build(self, output_path):
        """Build the HTML report file."""
        sections = self._get_sections_to_render()
        chart_configs = self._prepare_chart_configs()

        # Parse findings if provided as JSON string
        findings_list = []
        if self.findings:
            try:
                findings_list = json.loads(self.findings) if isinstance(self.findings, str) else self.findings
            except (json.JSONDecodeError, TypeError):
                findings_list = [{"text": self.findings}]

        context = {
            "metrics": self.metrics,
            "metrics_json": json.dumps(self.metrics, default=str),
            "deals_json": json.dumps(self.metrics.get("deals_list", []), default=str),
            "chart_configs_json": json.dumps(chart_configs, default=str),
            "sections": sections,
            "findings": findings_list,
            "generated_at": datetime.now().strftime("%B %d, %Y at %I:%M %p"),
            "generated_date": datetime.now().strftime("%Y-%m-%d"),
        }

        env = Environment(loader=FileSystemLoader(str(SCRIPT_DIR)))
        template = env.get_template("report_template.html")
        html = template.render(**context)

        Path(output_path).write_text(html, encoding="utf-8")
        return output_path
