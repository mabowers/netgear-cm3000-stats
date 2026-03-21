#!/usr/bin/env python3
"""Fetch CableInfo.txt from a NETGEAR CM3000, archive the raw file, and write to SQLite."""

import argparse
import datetime
import http.cookiejar
import os
import re
import sqlite3
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _make_opener():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )


def _get(opener, url):
    try:
        with opener.open(url, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} fetching {url}")
    except urllib.error.URLError as e:
        sys.exit(f"Error fetching {url}: {e.reason}")


def _post(opener, url, fields):
    data = urllib.parse.urlencode(fields).encode()
    try:
        with opener.open(url, data=data, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} posting to {url}")
    except urllib.error.URLError as e:
        sys.exit(f"Error posting to {url}: {e.reason}")


def _logout(opener, base_url):
    print("Logging out...", file=sys.stderr)
    try:
        _get(opener, base_url + "/Logout.htm")
    except SystemExit as e:
        print(f"Warning: logout failed: {e}", file=sys.stderr)


def fetch_cable_info(base_url, password):
    opener = _make_opener()

    print(f"Fetching login page from {base_url}...", file=sys.stderr)
    page = _get(opener, base_url + "/")

    match = re.search(r'action=["\']?(/goform/Login\?[^"\'> ]+)', page)
    if not match:
        sys.exit("Error: could not find login form action URL in page source.")
    login_url = base_url + match.group(1)

    print("Logging in...", file=sys.stderr)
    page = _post(opener, login_url, {"loginName": "admin", "loginPassword": password})
    if 'name="loginPassword"' in page:
        sys.exit("Error: login failed — check your password.")

    print("Downloading CableInfo.txt...", file=sys.stderr)
    try:
        content = _get(opener, base_url + "/CableInfo.txt")
    except SystemExit:
        _logout(opener, base_url)
        raise

    _logout(opener, base_url)
    return content


# ── Parsing ───────────────────────────────────────────────────────────────────

_SECTION_HEADERS = {
    "Cable Diagnostic",
    "Startup Procedure",
    "Downstream Bonded Channels",
    "Upstream Bonded Channels",
    "Downstream OFDM Channels",
    "Upstream OFDMA Channels",
    "Event Log",
}


def _split_sections(text):
    sections = {}
    current, buf = None, []
    for line in text.splitlines():
        if line.strip() in _SECTION_HEADERS:
            if current:
                sections[current] = buf
            current, buf = line.strip(), []
        elif current:
            buf.append(line)
    if current:
        sections[current] = buf
    return sections


def _parse_diag_section(lines):
    result = {"status": None, "cm_status": None, "ds_status": None, "us_status": None}
    for line in lines:
        s = line.strip()
        for key, pat in [
            ("status",    r"^Status:\s+(.+)"),
            ("cm_status", r"^CM Status:\s+(.+)"),
            ("ds_status", r"^Downstream Status:\s+(.+)"),
            ("us_status", r"^Upstream Status:\s+(.+)"),
        ]:
            m = re.match(pat, s)
            if m and result[key] is None:
                result[key] = m.group(1).strip()
    return result


def _parse_startup_section(lines):
    result = {
        "acquire_ds_freq_hz": None, "acquire_ds_status": None,
        "connectivity_state": None, "boot_state": None,
        "security": None, "ip_prov_mode": None,
    }
    for line in lines:
        s = line.strip()
        m = re.match(r"Acquire Downstream Channel:\s+(\d+)\s+Hz\s*(.*)", s)
        if m:
            result["acquire_ds_freq_hz"] = int(m.group(1))
            result["acquire_ds_status"] = m.group(2).strip() or None
            continue
        for key, pat in [
            ("connectivity_state", r"Connectivity State:\s+(.*)"),
            ("boot_state",         r"Boot State:\s*(.*)"),
            ("security",           r"Security:\s+(.*)"),
            ("ip_prov_mode",       r"IP Provisioning Mode:\s+(.*)"),
        ]:
            m = re.match(pat, s)
            if m:
                result[key] = " ".join(m.group(1).split()) or None
    return result


def _table_rows(lines):
    """Yield tokenized rows from a fixed-width table, skipping the header line."""
    header_skipped = False
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if not header_skipped:
            header_skipped = True
            continue  # first non-blank line is always the column header
        yield re.split(r"\s{2,}", s)


def _num(token):
    """Extract the leading number from a token like '42.8 dBmV' or '16400000 Hz'."""
    return token.split()[0]


def _parse_ds_channels(lines):
    rows = []
    for p in _table_rows(lines):
        if len(p) < 9:
            continue
        try:
            rows.append({
                "channel": int(p[0]), "lock_status": p[1], "modulation": p[2],
                "channel_id": int(p[3]), "freq_hz": int(_num(p[4])),
                "power_dbmv": float(p[5]), "snr_db": float(p[6]),
                "correctables": int(p[7]), "uncorrectables": int(p[8]),
            })
        except (ValueError, IndexError):
            pass
    return rows


def _parse_us_channels(lines):
    rows = []
    for p in _table_rows(lines):
        if len(p) < 7:
            continue
        try:
            rows.append({
                "channel": int(p[0]), "lock_status": p[1], "channel_type": p[2],
                "channel_id": int(p[3]), "symbol_rate_ksym": int(float(_num(p[4]))),
                "freq_hz": int(float(_num(p[5]))), "power_dbmv": float(_num(p[6])),
            })
        except (ValueError, IndexError):
            pass
    return rows


def _parse_ds_ofdm_channels(lines):
    # Columns: channel lock_status profile_ids channel_id freq power snr active_sub unerrored correctable uncorrectable
    rows = []
    for p in _table_rows(lines):
        if len(p) < 11:
            continue
        try:
            rows.append({
                "channel": int(p[0]), "lock_status": p[1],
                "profile_ids": p[2].replace(" ", ""),
                "channel_id": int(p[3]), "freq_hz": int(float(_num(p[4]))),
                "power_dbmv": float(_num(p[5])), "snr_db": float(_num(p[6])),
                "active_subcarrier": p[7],
                "unerrored": int(p[8]), "correctable": int(p[9]), "uncorrectable": int(p[10]),
            })
        except (ValueError, IndexError):
            pass
    return rows


def _parse_us_ofdma_channels(lines):
    # Columns: channel lock_status profile_ids channel_id freq power
    rows = []
    for p in _table_rows(lines):
        if len(p) < 6:
            continue
        try:
            rows.append({
                "channel": int(p[0]), "lock_status": p[1],
                "profile_ids": p[2].replace(" ", ""),
                "channel_id": int(p[3]), "freq_hz": int(float(_num(p[4]))),
                "power_dbmv": float(_num(p[5])),
            })
        except (ValueError, IndexError):
            pass
    return rows


def _parse_events(lines):
    # Columns separated by 5 spaces: timestamp     priority     description
    events = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        parts = s.split("     ", 2)  # 5 spaces is the column separator
        if len(parts) < 3 or not parts[1].strip() or parts[1].strip() == "Priority":
            continue
        events.append({
            "event_time": parts[0].strip(),
            "priority":   parts[1].strip(),
            "description": parts[2].strip(),
        })
    return events


def parse_cable_info(text):
    s = _split_sections(text)
    return {
        "diag":       _parse_diag_section(s.get("Cable Diagnostic", [])),
        "startup":    _parse_startup_section(s.get("Startup Procedure", [])),
        "ds_channels": _parse_ds_channels(s.get("Downstream Bonded Channels", [])),
        "us_channels": _parse_us_channels(s.get("Upstream Bonded Channels", [])),
        "ds_ofdm":    _parse_ds_ofdm_channels(s.get("Downstream OFDM Channels", [])),
        "us_ofdma":   _parse_us_ofdma_channels(s.get("Upstream OFDMA Channels", [])),
        "events":     _parse_events(s.get("Event Log", [])),
    }


# ── Database ──────────────────────────────────────────────────────────────────

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fetches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at          TEXT NOT NULL,
    status              TEXT,
    cm_status           TEXT,
    ds_status           TEXT,
    us_status           TEXT,
    acquire_ds_freq_hz  INTEGER,
    acquire_ds_status   TEXT,
    connectivity_state  TEXT,
    boot_state          TEXT,
    security            TEXT,
    ip_prov_mode        TEXT,
    raw_file            TEXT
);
CREATE TABLE IF NOT EXISTS ds_channels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_id        INTEGER NOT NULL REFERENCES fetches(id),
    channel         INTEGER,
    lock_status     TEXT,
    modulation      TEXT,
    channel_id      INTEGER,
    freq_hz         INTEGER,
    power_dbmv      REAL,
    snr_db          REAL,
    correctables    INTEGER,
    uncorrectables  INTEGER
);
CREATE TABLE IF NOT EXISTS us_channels (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_id         INTEGER NOT NULL REFERENCES fetches(id),
    channel          INTEGER,
    lock_status      TEXT,
    channel_type     TEXT,
    channel_id       INTEGER,
    symbol_rate_ksym INTEGER,
    freq_hz          INTEGER,
    power_dbmv       REAL
);
CREATE TABLE IF NOT EXISTS ds_ofdm_channels (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_id          INTEGER NOT NULL REFERENCES fetches(id),
    channel           INTEGER,
    lock_status       TEXT,
    profile_ids       TEXT,
    channel_id        INTEGER,
    freq_hz           INTEGER,
    power_dbmv        REAL,
    snr_db            REAL,
    active_subcarrier TEXT,
    unerrored         INTEGER,
    correctable       INTEGER,
    uncorrectable     INTEGER
);
CREATE TABLE IF NOT EXISTS us_ofdma_channels (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_id     INTEGER NOT NULL REFERENCES fetches(id),
    channel      INTEGER,
    lock_status  TEXT,
    profile_ids  TEXT,
    channel_id   INTEGER,
    freq_hz      INTEGER,
    power_dbmv   REAL
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_id    INTEGER NOT NULL REFERENCES fetches(id),
    event_time  TEXT,
    priority    TEXT,
    description TEXT
);
"""


def init_db(conn):
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current == 0:
        # Fresh database — create schema and stamp the version.
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()
    elif current != _SCHEMA_VERSION:
        raise SystemExit(
            f"Database schema version mismatch: file is v{current}, "
            f"expected v{_SCHEMA_VERSION}. "
            f"Delete cm3000.db and re-run to start fresh."
        )


def write_to_db(conn, fetched_at, parsed, raw_file):
    diag, startup = parsed["diag"], parsed["startup"]

    cur = conn.execute(
        """INSERT INTO fetches
           (fetched_at, status, cm_status, ds_status, us_status,
            acquire_ds_freq_hz, acquire_ds_status, connectivity_state,
            boot_state, security, ip_prov_mode, raw_file)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fetched_at,
         diag["status"], diag["cm_status"], diag["ds_status"], diag["us_status"],
         startup["acquire_ds_freq_hz"], startup["acquire_ds_status"],
         startup["connectivity_state"], startup["boot_state"],
         startup["security"], startup["ip_prov_mode"], raw_file),
    )
    fid = cur.lastrowid

    conn.executemany(
        """INSERT INTO ds_channels
           (fetch_id, channel, lock_status, modulation, channel_id,
            freq_hz, power_dbmv, snr_db, correctables, uncorrectables)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [(fid, r["channel"], r["lock_status"], r["modulation"], r["channel_id"],
          r["freq_hz"], r["power_dbmv"], r["snr_db"],
          r["correctables"], r["uncorrectables"]) for r in parsed["ds_channels"]],
    )
    conn.executemany(
        """INSERT INTO us_channels
           (fetch_id, channel, lock_status, channel_type, channel_id,
            symbol_rate_ksym, freq_hz, power_dbmv)
           VALUES (?,?,?,?,?,?,?,?)""",
        [(fid, r["channel"], r["lock_status"], r["channel_type"], r["channel_id"],
          r["symbol_rate_ksym"], r["freq_hz"], r["power_dbmv"]) for r in parsed["us_channels"]],
    )
    conn.executemany(
        """INSERT INTO ds_ofdm_channels
           (fetch_id, channel, lock_status, profile_ids, channel_id,
            freq_hz, power_dbmv, snr_db, active_subcarrier,
            unerrored, correctable, uncorrectable)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(fid, r["channel"], r["lock_status"], r["profile_ids"], r["channel_id"],
          r["freq_hz"], r["power_dbmv"], r["snr_db"], r["active_subcarrier"],
          r["unerrored"], r["correctable"], r["uncorrectable"]) for r in parsed["ds_ofdm"]],
    )
    conn.executemany(
        """INSERT INTO us_ofdma_channels
           (fetch_id, channel, lock_status, profile_ids, channel_id, freq_hz, power_dbmv)
           VALUES (?,?,?,?,?,?,?)""",
        [(fid, r["channel"], r["lock_status"], r["profile_ids"], r["channel_id"],
          r["freq_hz"], r["power_dbmv"]) for r in parsed["us_ofdma"]],
    )
    conn.executemany(
        "INSERT INTO events (fetch_id, event_time, priority, description) VALUES (?,?,?,?)",
        [(fid, r["event_time"], r["priority"], r["description"]) for r in parsed["events"]],
    )
    conn.commit()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch CableInfo.txt from a NETGEAR CM3000, archive it, and write to SQLite."
    )
    parser.add_argument("--url", default="https://192.168.100.1",
                        help="Base URL of the modem (default: https://192.168.100.1)")
    parser.add_argument("--password-file", metavar="FILE", default=".cm3000_password",
                        help="File containing the admin password (default: .cm3000_password)")
    parser.add_argument("--db", default="cm3000.db",
                        help="SQLite database path (default: cm3000.db)")
    parser.add_argument("--data-dir", default="data",
                        help="Directory for raw .txt archives (default: data/)")
    args = parser.parse_args()

    try:
        password = open(args.password_file, encoding="utf-8").read().strip()
    except OSError as e:
        sys.exit(f"Error reading password file: {e}")

    content = fetch_cable_info(args.url, password)

    fetched_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    filename = fetched_at.replace(":", "_") + "-CableInfo.txt"

    os.makedirs(args.data_dir, exist_ok=True)
    raw_path = os.path.join(args.data_dir, filename)
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Saved: {raw_path}", file=sys.stderr)

    parsed = parse_cable_info(content)

    conn = sqlite3.connect(args.db)
    try:
        init_db(conn)
        write_to_db(conn, fetched_at, parsed, raw_path)
        print(f"Written to: {args.db}", file=sys.stderr)
    finally:
        conn.close()


if __name__ == "__main__":
    main()