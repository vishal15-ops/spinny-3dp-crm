{% extends "base.html" %}
{% block title %}Wastage Report{% endblock %}
{% block content %}
<div class="page-header">
  <div>
    <div class="page-title">📊 Material Wastage Report</div>
    <div class="page-meta">For every "Issue" entry, wastage is calculated by matching actual material printed between that date and the next Issue entry</div>
  </div>
  <a href="/stock" style="text-decoration:none;font-size:13px;color:#e91e63;font-weight:600">← Back to Stock</a>
</div>

<div class="table-card" style="padding:14px 20px;margin-bottom:18px;background:#fef9c3">
  <div style="font-size:12.5px;color:#854d0e">
    <b>Note:</b> 🔄 <b>Ongoing</b> = this spool is still in use (no newer Issue yet) — the leftover is just "Remaining", not wastage, since printing may still use it up. ✅ <b>Closed</b> = a newer Issue has been logged, so this spool is treated as finished — the leftover is the true "Wastage". Green = 0-10% wastage (normal), Orange = 10-25% (slightly high), Red = 25%+ (needs checking), Purple = more used than issued (possibly an incorrect spool size entry).
  </div>
</div>

<div class="table-card">
  <div class="table-card-header">
    <span>Spool-wise Wastage</span>
  </div>
  <table>
    <thead><tr><th>Period</th><th>Status</th><th>City</th><th>Material</th><th style="text-align:center">Issued</th><th style="text-align:center">Actual Used</th><th style="text-align:center">Remaining / Wastage</th><th style="text-align:center">%</th></tr></thead>
    <tbody>{{ wastage_html|safe }}</tbody>
  </table>
</div>
{% endblock %}
