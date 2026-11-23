# Prisma SD-WAN: patch DHCP relay IPs

`patch_dhcp_relay.py` adds DHCP relay server IPs to specific interfaces (by default 6.23–6.29) on Prisma SD-WAN IONs. The sites, IONs and IPs to use come from a CSV file. The new IPs are added after the IPs already configured, and an IP that is already there isn't added again. Interfaces not on the list are never changed.

## Requirements

```
pip install prisma_sase
```

The script logs in with a Prisma SASE service account:

```
export PRISMASDWAN_CLIENT_ID=...
export PRISMASDWAN_CLIENT_SECRET=...
export PRISMASDWAN_TSG_ID=...
```

## CSV format

```
site,element,relay_ips
Branch-Seattle,,192.168.1.1;192.168.1.2
Branch-Portland,PDX-ION-01,192.168.1.1;192.168.1.2
```

| Column | Meaning |
|---|---|
| `site` | Site name or site ID (required) |
| `element` | ION name or element ID (optional; leave blank to update every ION at the site) |
| `relay_ips` | IPs to add, separated by `;` or spaces (required) |

The script checks the whole CSV before it logs in. If any row has an error, it stops and changes nothing.

## Usage

```
python3 patch_dhcp_relay.py --csv sites.csv --dry-run    # preview, no changes
python3 patch_dhcp_relay.py --csv sites.csv              # commit
```

| Option | Effect |
|---|---|
| `--dry-run`, `-n` | Show the before and after for each interface without committing |
| `--interfaces`, `-i` | Interface names to update (default: `6.23 6.24 6.25 6.26 6.27 6.28 6.29`) |
| `--include-unconfigured` | Also turn on DHCP relay for target interfaces that have none configured (skipped by default) |
| `--log-file` | File that gets a copy of all output (default: `debug.txt`, replaced on each run) |
| `--verbose`, `-v` | Print the full `dhcp_relay` JSON that will be sent |

The script exits with code 1 if any update fails or any CSV row can't be matched to a site or ION.

Always run with `--dry-run` first. Relay changes take effect on the ION right away.
