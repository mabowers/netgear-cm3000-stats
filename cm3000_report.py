#!/usr/bin/env python3
"""Generate a self-contained HTML status report from cm3000.db."""

__version__ = "1.0.0"

import argparse
import datetime
import html
import sqlite3
import sys


# ── Color-coding helpers ──────────────────────────────────────────────────────
# Three levels matching the modem's own language: Good (green) / Poor (amber) / Bad (red)

def _status_cls(s):
    if not s: return ""
    s = s.lower()
    if s == "good": return "good"
    if s == "poor": return "poor"
    if s == "bad":  return "bad"
    return ""

def _lock_cls(s):
    if s == "Locked":     return "good"
    if s == "Not Locked": return "na"
    return ""

def _ds_power_cls(v):
    """DS QAM/OFDM power: recommended −10 to 10 dBmV."""
    if v is None: return "na"
    if -10 <= v <= 10:  return "good"
    if -15 <= v <= 15:  return "poor"
    return "bad"

def _qam_snr_cls(v):
    """QAM DS SNR: ≥33 dB good (DOCSIS 3.0)."""
    if v is None: return "na"
    if v >= 33:   return "good"
    if v >= 30:   return "poor"
    return "bad"

def _ofdm_snr_cls(v):
    """OFDM DS MER: ≥36 dB good (DOCSIS 3.1 higher modulation order)."""
    if v is None: return "na"
    if v >= 36:   return "good"
    if v >= 33:   return "poor"
    return "bad"

def _us_power_cls(v):
    """US bonded/OFDMA power: recommended 35–48 dBmV."""
    if v is None: return "na"
    if 35 <= v <= 48:  return "good"
    if 32 <= v <= 51:  return "poor"
    return "bad"

def _priority_cls(s):
    if not s: return ""
    s = s.lower()
    if "critical" in s or "emergency" in s or "alert" in s: return "bad"
    if "warning"  in s or "error"     in s:                 return "poor"
    return ""

def _connected_cls(connectivity_state):
    """Green if DOCSIS connectivity is Operational, red otherwise."""
    if not connectivity_state: return "na"
    return "good" if "operational" in connectivity_state.lower() else "bad"


# ── HTML primitives ───────────────────────────────────────────────────────────

def _esc(v):
    return html.escape(str(v)) if v is not None else "&mdash;"

def _td(v, cls=""):
    c = f' class="{cls.strip()}"' if cls.strip() else ""
    return f"<td{c}>{_esc(v)}</td>"

def _th(*headers):
    return "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>\n"

def _fmt_ts(ts, tz=False):
    """Format a stored ISO timestamp. tz=True appends the local timezone abbreviation."""
    try:
        dt = datetime.datetime.fromisoformat(ts)
        if tz and dt.tzinfo:
            dt = dt.astimezone()   # resolve numeric offset → named local zone (e.g. EDT)
        fmt = "%b %d %H:%M:%S" + (" %Z" if tz and dt.tzinfo else "")
        return dt.strftime(fmt).strip()
    except (ValueError, TypeError):
        return ts or "—"

def _fmt_mhz(hz):
    return f"{hz / 1_000_000:.1f}" if hz else "—"

def _f1(v):
    return f"{v:.1f}" if v is not None else "—"



# ── Report sections ───────────────────────────────────────────────────────────

def _history_table(rows):
    if not rows:
        return "<p>No fetch history found.</p>"
    # Three-tier header: direction → DOCSIS version → metric.
    # QAM/Bonded: averaged across all locked channels. OFDM/OFDMA: single channel.
    header = (
        "<tr>"
        '<th rowspan="3">Time</th>'
        '<th rowspan="3">Overall<br>Status</th>'
        '<th rowspan="3">Connected</th>'
        '<th colspan="5" class="hdr-group sep">Downstream &#8595;</th>'
        '<th colspan="3" class="hdr-group sep">Upstream &#8593;</th>'
        "</tr>\n<tr>"
        '<th rowspan="2" class="sep">Status</th>'
        '<th colspan="2">Bonded (3.0)</th>'
        '<th colspan="2">OFDM (3.1)</th>'
        '<th rowspan="2" class="sep">Status</th>'
        "<th>Bonded (3.0)</th>"
        "<th>OFDMA (3.1)</th>"
        "</tr>\n<tr>"
        "<th>Power (dBmV)</th>"
        "<th>SNR (dB)</th>"
        "<th>Power (dBmV)</th>"
        "<th>SNR (dB)</th>"
        "<th>Power (dBmV)</th>"
        "<th>Power (dBmV)</th>"
        "</tr>\n"
    )
    out = ['<table class="history">', header]
    for (fid, fetched_at, status, conn_state, ds, us,
         qam_avg_p, qam_avg_snr,
         ofdm_p, ofdm_snr,
         bonded_avg_p, ofdma_p) in rows:
        out.append("<tr>")
        out.append(_td(_fmt_ts(fetched_at), "mono"))
        out.append(_td(status or "—", _status_cls(status)))
        out.append(_td("Yes" if conn_state and "operational" in conn_state.lower() else (conn_state or "—"),
                       _connected_cls(conn_state)))
        # Downstream group
        out.append(_td(ds or "—", "sep " + _status_cls(ds)))
        out.append(_td(_f1(qam_avg_p),   _ds_power_cls(qam_avg_p)    + " mono"))
        out.append(_td(_f1(qam_avg_snr), _qam_snr_cls(qam_avg_snr)   + " mono"))
        out.append(_td(_f1(ofdm_p),      _ds_power_cls(ofdm_p)       + " mono"))
        out.append(_td(_f1(ofdm_snr),    _ofdm_snr_cls(ofdm_snr)     + " mono"))
        # Upstream group
        out.append(_td(us or "—", "sep " + _status_cls(us)))
        out.append(_td(_f1(bonded_avg_p), _us_power_cls(bonded_avg_p) + " mono"))
        out.append(_td(_f1(ofdma_p),      _us_power_cls(ofdma_p)      + " mono"))
        out.append("</tr>\n")
    out.append("</table>")
    return "\n".join(out)


def _startup_table(fetch):
    if not fetch:
        return "<p>No data.</p>"
    (fid, fetched_at, status, cm, ds, us,
     acq_freq, acq_status, conn, boot, sec, ip, raw) = fetch
    rows = [
        ("Acquire Downstream Channel",
         f"{_fmt_mhz(acq_freq)} MHz" if acq_freq else "—", acq_status or "—"),
        ("Connectivity State",   conn or "—", ""),
        ("Boot State",           boot or "—", ""),
        ("Security",             sec  or "—", ""),
        ("IP Provisioning Mode", ip   or "—", ""),
    ]
    out = ["<table style='width: auto;'>", _th("Procedure", "Value", "Status")]
    for label, val, extra in rows:
        out.append(f"<tr><td>{label}</td>"
                   f'<td class="mono">{_esc(val)}</td>'
                   f"<td>{_esc(extra)}</td></tr>\n")
    out.append("</table>")
    return "\n".join(out)


def _ds_channels_table(rows):
    if not rows:
        return "<p>No data.</p>"
    out = ['<table class="channel-table">',
           _th("Ch", "Lock", "Mod", "ID", "Freq (MHz)",
               "Power (dBmV)", "SNR (dB)", "Correctables", "Uncorrectables")]
    for (rid, fid, ch, lock, mod, ch_id, freq, pwr, snr, corr, uncorr) in rows:
        locked = lock == "Locked"
        out.append("<tr>")
        out.append(_td(ch))
        out.append(_td(lock, _lock_cls(lock)))
        out.append(_td(mod or "—"))
        out.append(_td(ch_id if locked else "—"))
        out.append(_td(_fmt_mhz(freq) if locked else "—", "mono"))
        out.append(_td(_f1(pwr) if locked else "—", (_ds_power_cls(pwr) + " mono") if locked else "na mono"))
        out.append(_td(_f1(snr) if locked else "—", (_qam_snr_cls(snr)   + " mono") if locked else "na mono"))
        out.append(_td(corr   if locked else "—", "mono"))
        out.append(_td(uncorr if locked else "—", "mono"))
        out.append("</tr>\n")
    out.append("</table>")
    return "\n".join(out)


def _us_channels_table(rows):
    if not rows:
        return "<p>No data.</p>"
    out = ['<table class="channel-table">',
           _th("Ch", "Lock", "Type", "ID", "Symbol Rate (Ksym/s)",
               "Freq (MHz)", "Power (dBmV)")]
    for (rid, fid, ch, lock, ch_type, ch_id, sym, freq, pwr) in rows:
        locked = lock == "Locked"
        out.append("<tr>")
        out.append(_td(ch))
        out.append(_td(lock, _lock_cls(lock)))
        out.append(_td(ch_type or "—"))
        out.append(_td(ch_id if locked else "—"))
        out.append(_td(sym    if locked else "—", "mono"))
        out.append(_td(_fmt_mhz(freq) if locked else "—", "mono"))
        out.append(_td(_f1(pwr) if locked else "—", (_us_power_cls(pwr) + " mono") if locked else "na mono"))
        out.append("</tr>\n")
    out.append("</table>")
    return "\n".join(out)


def _ds_ofdm_table(rows):
    if not rows:
        return "<p>No data.</p>"
    out = ['<table class="channel-table">',
           _th("Ch", "Lock", "Profiles", "ID", "Freq (MHz)", "Power (dBmV)",
               "SNR/MER (dB)", "Subcarriers", "Unerrored", "Correctable", "Uncorrectable")]
    for (rid, fid, ch, lock, profiles, ch_id, freq, pwr, snr,
         active_sub, unerr, corr, uncorr) in rows:
        locked = lock == "Locked"
        out.append("<tr>")
        out.append(_td(ch))
        out.append(_td(lock, _lock_cls(lock)))
        out.append(_td(profiles or "—", "mono"))
        out.append(_td(ch_id if locked else "—"))
        out.append(_td(_fmt_mhz(freq) if locked else "—", "mono"))
        out.append(_td(_f1(pwr) if locked else "—", (_ds_power_cls(pwr) + " mono") if locked else "na mono"))
        out.append(_td(_f1(snr) if locked else "—", (_ofdm_snr_cls(snr)  + " mono") if locked else "na mono"))
        out.append(_td(active_sub if locked else "—", "mono"))
        out.append(_td(unerr  if locked else "—", "mono"))
        out.append(_td(corr   if locked else "—", "mono"))
        out.append(_td(uncorr if locked else "—", "mono"))
        out.append("</tr>\n")
    out.append("</table>")
    return "\n".join(out)


def _us_ofdma_table(rows):
    if not rows:
        return "<p>No data.</p>"
    out = ['<table class="channel-table">',
           _th("Ch", "Lock", "Profiles", "ID", "Freq (MHz)", "Power (dBmV)")]
    for (rid, fid, ch, lock, profiles, ch_id, freq, pwr) in rows:
        locked = lock == "Locked"
        out.append("<tr>")
        out.append(_td(ch))
        out.append(_td(lock, _lock_cls(lock)))
        out.append(_td(profiles or "—", "mono"))
        out.append(_td(ch_id if locked else "—"))
        out.append(_td(_fmt_mhz(freq) if locked else "—", "mono"))
        out.append(_td(_f1(pwr) if locked else "—", (_us_power_cls(pwr) + " mono") if locked else "na mono"))
        out.append("</tr>\n")
    out.append("</table>")
    return "\n".join(out)


def _events_table(rows):
    if not rows:
        return "<p>No events.</p>"
    # Rows are in CableInfo.txt order (insertion order); preserve that sequence.
    out = ['<table class="events-table">', _th("Time", "Priority", "Description")]
    for (eid, event_time, priority, description) in rows:
        out.append("<tr>")
        out.append(_td(event_time  or "—", "mono"))
        out.append(_td(priority    or "—", _priority_cls(priority)))
        out.append(_td(description or "—"))
        out.append("</tr>\n")
    out.append("</table>")
    return "\n".join(out)


# ── CSS ───────────────────────────────────────────────────────────────────────

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 14px;
    background: #f5f5f5;
    color: #222;
    padding: 24px;
}

h1 { font-size: 22px; margin-bottom: 4px; }
h2 { font-size: 16px; margin: 28px 0 10px; border-bottom: 2px solid #3a5a8c;
     padding-bottom: 4px; color: #3a5a8c; }
h3 { font-size: 14px; margin: 20px 0 8px; color: #555; }
.meta { color: #666; font-size: 12px; margin-bottom: 8px; }

/* Legend color swatches */
.legend { display: flex; gap: 12px; margin: 12px 0 20px; flex-wrap: wrap; }
.legend span {
    padding: 3px 10px;
    border-radius: 3px;
    font-size: 12px;
    font-weight: 500;
}

.scroll { overflow-x: auto; margin-bottom: 4px; }

table {
    border-collapse: collapse;
    width: 100%;
    background: #fff;
    border-radius: 4px;
    box-shadow: 0 1px 3px rgba(0,0,0,.1);
}

th {
    background: #3a5a8c;
    color: #fff;
    padding: 7px 10px;
    text-align: center;
    font-weight: 600;
    font-size: 12px;
    vertical-align: middle;
}

td {
    padding: 5px 10px;
    border-bottom: 1px solid #e8e8e8;
    white-space: nowrap;
    font-size: 13px;
}

/* Center everything in the fetch history and channel detail tables */
table.history th, table.history td,
table.channel-table td { text-align: center; }

/* Event log: smaller font, description column wraps */
table.events-table { font-size: 12px; }
table.events-table td:last-child { white-space: normal; }

footer {
    margin-top: 40px;
    padding-top: 12px;
    border-top: 1px solid #ddd;
    color: #999;
    font-size: 11px;
    text-align: center;
    line-height: 1.6;
}
footer a { color: #999; }

tr:last-child td { border-bottom: none; }

tr:nth-child(even) td { background: #fafafa; }

/* Color classes — applied to both td and legend spans */
.good     { background-color: #d4edda !important; color: #155724; }
.poor     { background-color: #fff3cd !important; color: #856404; }
.bad      { background-color: #f8d7da !important; color: #721c24; }
.na       { background-color: #e9ecef !important; color: #888;    }

.mono { font-family: "SF Mono", "Fira Code", "Consolas", monospace; font-size: 12px; }

/* History table group header (Downstream / Upstream) */
th.hdr-group { background: #2c4470; }

/* Group separator — white gap between Modem/DS and DS/US columns */
td.sep { border-left: 3px solid #ccc; }
th.sep { border-left: 3px solid rgba(255,255,255,0.35); }

@media print {
    @page { margin: 1cm; }
    /* Force background colors (good/poor/bad cells) to print */
    * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    body  { padding: 0; background: #fff; }
    h1    { font-size: 16pt; }
    /* Keep headings on the same page as the content that follows them */
    h2    { font-size: 11pt; margin-top: 14pt; break-after: avoid; page-break-after: avoid; }
    h3    { font-size: 9pt; break-after: avoid; page-break-after: avoid; }
    .meta { font-size: 7.5pt; }
    /* Let the scroll wrappers expand to full table width */
    .scroll { overflow: visible; }
    /* Tables can span pages; individual rows should not split mid-row */
    table   { box-shadow: none; }
    tr      { break-inside: avoid; page-break-inside: avoid; }
    th      { font-size: 7pt; padding: 3px 5px; white-space: normal; }
    td      { font-size: 8pt; padding: 3px 5px; white-space: normal; }
    .mono   { font-size: 7.5pt; }
    footer  { margin-top: 16pt; font-size: 8pt; }
}
"""


# ── Page assembly ─────────────────────────────────────────────────────────────

def _legend():
    return """<div class="legend">
  <span class="good">Good / in range</span>
  <span class="poor">Poor / marginal</span>
  <span class="bad">Bad / out of range</span>
  <span class="na">Not locked / N/A</span>
</div>"""


def build_report(conn, hours):
    cutoff = (datetime.datetime.now().astimezone() - datetime.timedelta(hours=hours)
              ).isoformat(timespec="seconds")

    # Fetch history: average locked channels per signal type.
    # QAM/Bonded: AVG across all locked channels (32 DS QAM, 4 US bonded).
    # OFDM/OFDMA: single locked channel, so MIN() == the value.
    history = conn.execute("""
        SELECT f.id, f.fetched_at, f.status, f.connectivity_state,
               f.ds_status, f.us_status,
               AVG(d.power_dbmv),  AVG(d.snr_db),
               MIN(o.power_dbmv),  MIN(o.snr_db),
               AVG(u.power_dbmv),
               MIN(a.power_dbmv)
        FROM fetches f
        LEFT JOIN ds_channels       d ON d.fetch_id = f.id AND d.lock_status = 'Locked'
        LEFT JOIN ds_ofdm_channels  o ON o.fetch_id = f.id AND o.lock_status = 'Locked'
        LEFT JOIN us_channels       u ON u.fetch_id = f.id AND u.lock_status = 'Locked'
        LEFT JOIN us_ofdma_channels a ON a.fetch_id = f.id AND a.lock_status = 'Locked'
        WHERE f.fetched_at >= ?
        GROUP BY f.id
        ORDER BY f.fetched_at DESC
    """, (cutoff,)).fetchall()

    latest = conn.execute("""
        SELECT id, fetched_at, status, cm_status, ds_status, us_status,
               acquire_ds_freq_hz, acquire_ds_status, connectivity_state,
               boot_state, security, ip_prov_mode, raw_file
        FROM fetches ORDER BY fetched_at DESC LIMIT 1
    """).fetchone()

    latest_id = latest[0] if latest else None
    latest_ts = _fmt_ts(latest[1], tz=True) if latest else "—"

    def latest_rows(table):
        if latest_id is None:
            return []
        return conn.execute(
            f"SELECT * FROM {table} WHERE fetch_id = ? ORDER BY channel",
            (latest_id,)
        ).fetchall()

    events = conn.execute(
        "SELECT id, event_time, priority, description FROM events WHERE fetch_id = ? ORDER BY id ASC",
        (latest_id,)
    ).fetchall() if latest_id else []

    generated = datetime.datetime.now().astimezone().strftime("%b %d %H:%M:%S %Z")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CM3000 Status Report</title>
<style>{_CSS}</style>
</head>
<body>
<h1>NETGEAR CM3000 Status Report</h1>
<p class="meta">Generated: {generated} &nbsp;&bull;&nbsp; Latest Snapshot: {latest_ts}</p>

{_legend()}

<h2>Modem History &mdash; Last {hours} Hours</h2>
<p class="meta">
  <u>DS power</u>: &plusmn;10&nbsp;dBmV good, &plusmn;15 poor. <u>Bonded SNR</u>: &ge;33&nbsp;dB good, &ge;30 poor. <u>OFDM SNR</u>: &ge;36&nbsp;dB good, &ge;33 poor. <u>US power</u>: 35&ndash;48&nbsp;dBmV good, 32&ndash;51 poor.<br>
  Bonded values are averaged across all locked channels. OFDM/OFDMA are the primary DOCSIS&nbsp;3.1 channels.
</p>
<div class="scroll">
{_history_table(history)}
</div>

<h2>Latest Snapshot &mdash; {latest_ts}</h2>

<h3>Startup Procedure</h3>
{_startup_table(latest)}

<h3>Downstream Bonded Channels (DOCSIS 3.0)</h3>
<div class="scroll">
{_ds_channels_table(latest_rows("ds_channels"))}
</div>

<h3>Upstream Bonded Channels (DOCSIS 3.0)</h3>
<div class="scroll">
{_us_channels_table(latest_rows("us_channels"))}
</div>

<h3>Downstream OFDM Channels (DOCSIS 3.1)</h3>
<div class="scroll">
{_ds_ofdm_table(latest_rows("ds_ofdm_channels"))}
</div>

<h3>Upstream OFDMA Channels (DOCSIS 3.1)</h3>
<div class="scroll">
{_us_ofdma_table(latest_rows("us_ofdma_channels"))}
</div>

<h2>Event Log</h2>
<p class="meta">In the order recorded by the modem. &ldquo;Time Not Established&rdquo; entries appear where the modem logged them (before clock sync).</p>
<div class="scroll">
{_events_table(events)}
</div>

<footer>
  <a href="https://github.com/mabowers/netgear-cm3000-stats">github.com/mabowers/netgear-cm3000-stats</a><br>
  v{__version__}
</footer>

</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate an HTML status report from cm3000.db."
    )
    parser.add_argument("--db", default="cm3000.db",
                        help="SQLite database path (default: cm3000.db)")
    parser.add_argument("--hours", type=int, default=24,
                        help="Hours of fetch history to show (default: 24)")
    parser.add_argument("-o", "--output", default="report.html",
                        help='Output file, or "-" for stdout (default: report.html)')
    args = parser.parse_args()

    try:
        conn = sqlite3.connect(args.db)
    except sqlite3.OperationalError as e:
        sys.exit(f"Cannot open database: {e}")

    try:
        report = build_report(conn, args.hours)
    finally:
        conn.close()

    if args.output == "-":
        print(report)
    else:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
