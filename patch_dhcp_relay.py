#!/usr/bin/env python3
"""
patch_dhcp_relay.py

Add DHCP relay server IPs to specific interfaces (by default 6.23 - 6.29)
on Prisma SD-WAN IONs, driven by a CSV file. Other interfaces are never
touched. New IPs are appended to each interface's existing
dhcp_relay.server_ips, and IPs already present are not added twice.

CSV format (header row required):
    site,element,relay_ips
    Branch-Seattle,,192.168.1.1;192.168.1.2
    Branch-Portland,PDX-ION-01,10.10.10.5;10.10.10.6
    1700000000000123456,,192.168.1.1

    site       Site name or site ID (required)
    element    ION name or element ID (optional). Leave blank to update
               every ION at the site.
    relay_ips  IPs to add, separated by ';' (or spaces). Required.

Rows for the same site/ION are merged, so their IPs are combined.

Requirements:
    pip install prisma_sase

Authentication uses a Prisma SASE service account. Set these environment
variables, or pass the matching command-line flags:
    PRISMASDWAN_CLIENT_ID
    PRISMASDWAN_CLIENT_SECRET
    PRISMASDWAN_TSG_ID

Examples:
    ./patch_dhcp_relay.py --csv sites.csv --dry-run
    ./patch_dhcp_relay.py --csv sites.csv
    ./patch_dhcp_relay.py --csv sites.csv --include-unconfigured --dry-run
    ./patch_dhcp_relay.py --csv sites.csv --interfaces 6.23 6.24 --dry-run
"""

import argparse
import copy
import csv
import datetime
import ipaddress
import json
import os
import re
import sys
from collections import OrderedDict

try:
    import prisma_sase
except ImportError:
    sys.exit("ERROR: prisma_sase SDK not installed. Run: pip install prisma_sase")

# All console output (stdout and stderr) is also written to this file
LOG_FILE = "debug.txt"

# Only these interfaces (matched by interface name) are updated
TARGET_INTERFACES = ["6.23", "6.24", "6.25", "6.26", "6.27", "6.28", "6.29"]


def parse_args():
    p = argparse.ArgumentParser(
        description="Append DHCP relay server IPs to selected interfaces on IONs listed in a CSV."
    )
    p.add_argument("--interfaces", "-i", nargs="+", default=TARGET_INTERFACES,
                   help=f"Interface names to update (default: {' '.join(TARGET_INTERFACES)})")
    p.add_argument("--csv", "-c", required=True,
                   help="CSV file with columns: site, element (optional), relay_ips")
    p.add_argument("--dry-run", "-n", action="store_true",
                   help="Show the changes without committing them")
    p.add_argument("--include-unconfigured", action="store_true",
                   help="Also enable DHCP relay on target interfaces that have none "
                        "configured (default: skip them)")
    p.add_argument("--client-id", default=os.environ.get("PRISMASDWAN_CLIENT_ID"))
    p.add_argument("--client-secret", default=os.environ.get("PRISMASDWAN_CLIENT_SECRET"))
    p.add_argument("--tsg-id", default=os.environ.get("PRISMASDWAN_TSG_ID"))
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Print the full dhcp_relay JSON that would be sent")
    p.add_argument("--log-file", default=LOG_FILE,
                   help=f"File that receives a copy of all output (default: {LOG_FILE})")
    return p.parse_args()


class Tee:
    """Write everything to the original stream and to the log file."""
    def __init__(self, stream, logfile):
        self.stream = stream
        self.logfile = logfile

    def write(self, data):
        self.stream.write(data)
        self.logfile.write(data)
        self.logfile.flush()

    def flush(self):
        self.stream.flush()
        self.logfile.flush()


def start_logging(path):
    """Send all stdout/stderr to the console and to the log file (overwritten each run)."""
    logfile = open(path, "w", encoding="utf-8")
    sys.stdout = Tee(sys.__stdout__, logfile)
    sys.stderr = Tee(sys.__stderr__, logfile)
    print(f"Run started {datetime.datetime.now().isoformat(timespec='seconds')}")
    print(f"Command: {' '.join(sys.argv)}")


def die(msg, resp=None):
    print(f"ERROR: {msg}", file=sys.stderr)
    if resp is not None:
        print_resp_error(resp)
    sys.exit(1)


def print_resp_error(resp):
    try:
        print(json.dumps(resp.cgx_content, indent=2), file=sys.stderr)
    except Exception:
        print(getattr(resp, "text", resp), file=sys.stderr)


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def load_csv(path):
    """
    Returns an OrderedDict keyed by (site, element_or_None) -> list of IPs.
    Exits on any invalid row so nothing is changed from a bad file.
    """
    if not os.path.isfile(path):
        die(f"CSV file '{path}' not found.")

    targets = OrderedDict()
    errors = []

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            die("CSV file is empty.")
        cols = {c.strip().lower(): c for c in reader.fieldnames if c}
        for required in ("site", "relay_ips"):
            if required not in cols:
                die(f"CSV is missing required column '{required}'. "
                    f"Found: {', '.join(reader.fieldnames)}")

        for lineno, row in enumerate(reader, start=2):
            get = lambda k: (row.get(cols[k]) or "").strip() if k in cols else ""
            site = get("site")
            element = get("element") or None
            raw_ips = get("relay_ips")

            if not site and not raw_ips:
                continue  # blank line
            if site.startswith("#"):
                continue  # comment line
            if not site:
                errors.append(f"line {lineno}: 'site' is empty")
                continue
            if not raw_ips:
                errors.append(f"line {lineno}: 'relay_ips' is empty")
                continue

            ips = [i for i in re.split(r"[;\s|]+", raw_ips) if i]
            bad = []
            for ip in ips:
                try:
                    ipaddress.ip_address(ip)
                except ValueError:
                    bad.append(ip)
            if bad:
                errors.append(f"line {lineno}: invalid IP(s) {', '.join(bad)}")
                continue

            key = (site, element)
            merged = targets.setdefault(key, [])
            for ip in ips:
                if ip not in merged:
                    merged.append(ip)

    if errors:
        die("CSV has errors, nothing was changed:\n  " + "\n  ".join(errors))
    if not targets:
        die("CSV contains no rows to process.")
    return targets


# --------------------------------------------------------------------------- #
# API helpers
# --------------------------------------------------------------------------- #
def login(args):
    if not all([args.client_id, args.client_secret, args.tsg_id]):
        die("Missing credentials. Set PRISMASDWAN_CLIENT_ID, "
            "PRISMASDWAN_CLIENT_SECRET and PRISMASDWAN_TSG_ID.")
    sdk = prisma_sase.API(ssl_verify=True)
    ok = sdk.interactive.login_secret(
        client_id=args.client_id,
        client_secret=args.client_secret,
        tsg_id=args.tsg_id,
    )
    if not ok:
        die("Login failed. Check the service account credentials and TSG ID.")
    return sdk


def get_items(resp, what):
    if not resp.cgx_status:
        die(f"Could not retrieve {what}.", resp)
    return resp.cgx_content.get("items", [])


def resolve_targets(sdk, targets):
    """
    Turn CSV (site, element) keys into a list of
    (site_dict, element_dict, [ips]) with IPs merged per element.
    Unresolvable rows are reported and skipped.
    """
    sites = get_items(sdk.get.sites(), "sites")
    elements = get_items(sdk.get.elements(), "elements")

    site_by_key = {}
    for s in sites:
        site_by_key[s["id"]] = s
        site_by_key.setdefault(s.get("name"), s)

    resolved = OrderedDict()   # element_id -> (site, element, ips)
    problems = []

    for (site_key, el_key), ips in targets.items():
        site = site_by_key.get(site_key)
        if not site:
            problems.append(f"site '{site_key}' not found")
            continue

        site_els = [e for e in elements if e.get("site_id") == site["id"]]
        if el_key:
            site_els = [e for e in site_els
                        if e.get("id") == el_key or e.get("name") == el_key]
            if not site_els:
                problems.append(f"ION '{el_key}' not found at site '{site.get('name')}'")
                continue
        elif not site_els:
            problems.append(f"no IONs assigned to site '{site.get('name')}'")
            continue

        for el in site_els:
            entry = resolved.setdefault(el["id"], (site, el, []))
            for ip in ips:
                if ip not in entry[2]:
                    entry[2].append(ip)

    return list(resolved.values()), problems


# --------------------------------------------------------------------------- #
# Patch logic
# --------------------------------------------------------------------------- #
def process_element(sdk, site, element, ips, args, totals):
    site_id, element_id = site["id"], element["id"]
    print(f"\n=== Site: {site.get('name')} | ION: {element.get('name')} "
          f"({element_id})")
    print(f"    Relay IPs to add: {', '.join(ips)}")

    resp = sdk.get.interfaces(site_id, element_id)
    if not resp.cgx_status:
        print("    FAILED to retrieve interfaces", file=sys.stderr)
        print_resp_error(resp)
        totals["failed"] += 1
        return

    wanted = list(dict.fromkeys(args.interfaces))
    by_name = {i.get("name"): i for i in resp.cgx_content.get("items", [])}

    missing = [n for n in wanted if n not in by_name]
    for n in missing:
        print(f"    MISSING   {n}: interface not found on this ION")
    totals["missing"] += len(missing)

    target_intfs = [by_name[n] for n in wanted if n in by_name]
    if not target_intfs:
        print("    None of the target interfaces exist on this ION.")
        return

    for intf in target_intfs:
        name = intf.get("name", intf.get("id"))
        relay = intf.get("dhcp_relay") or {}
        existing = list(relay.get("server_ips") or [])
        configured = bool(relay) and (relay.get("enabled") or existing)

        if not configured and not args.include_unconfigured:
            print(f"    SKIP      {name}: no DHCP relay configured "
                  f"(use --include-unconfigured to add one)")
            totals["skipped"] += 1
            continue

        to_add = [ip for ip in ips if ip not in existing]
        if not to_add and relay.get("enabled"):
            print(f"    NO CHANGE {name}: already has {', '.join(existing)}")
            totals["unchanged"] += 1
            continue

        new_ips = existing + to_add
        new_intf = copy.deepcopy(intf)
        new_relay = copy.deepcopy(relay)
        new_relay["enabled"] = True
        new_relay["server_ips"] = new_ips
        new_relay.setdefault("source_interface", None)
        new_relay.setdefault("option_82", None)
        new_intf["dhcp_relay"] = new_relay

        print(f"    CHANGE    {name}")
        print(f"                before: {existing or '(none)'}")
        print(f"                after:  {new_ips}")
        if args.verbose:
            print(json.dumps(new_relay, indent=4))

        if args.dry_run:
            totals["changed"] += 1
            continue

        put = sdk.put.interfaces(site_id, element_id, intf["id"], new_intf)
        if put.cgx_status:
            print("                -> committed")
            totals["changed"] += 1
        else:
            print("                -> FAILED", file=sys.stderr)
            print_resp_error(put)
            totals["failed"] += 1


def main():
    args = parse_args()
    start_logging(args.log_file)
    targets = load_csv(args.csv)   # validate the file before logging in

    sdk = login(args)
    work, problems = resolve_targets(sdk, targets)

    mode = "DRY RUN - no changes will be committed" if args.dry_run else "LIVE"
    print(f"\n[{mode}]")
    print(f"CSV: {args.csv} ({len(targets)} row(s) -> {len(work)} ION(s))")
    print(f"Interfaces: {', '.join(args.interfaces)}")
    for p in problems:
        print(f"  WARNING: {p}")

    totals = {"changed": 0, "unchanged": 0, "skipped": 0, "failed": 0,
              "missing": 0}
    for site, element, ips in work:
        process_element(sdk, site, element, ips, args, totals)

    verb = "would change" if args.dry_run else "changed"
    print(f"\nSummary: {totals['changed']} interface(s) {verb}, "
          f"{totals['unchanged']} unchanged, {totals['skipped']} skipped, "
          f"{totals['missing']} not found, {totals['failed']} failed, "
          f"{len(problems)} CSV row(s) unresolved")
    if args.dry_run and totals["changed"]:
        print("Re-run without --dry-run to commit.")

    sys.exit(1 if (totals["failed"] or problems) else 0)


if __name__ == "__main__":
    main()
