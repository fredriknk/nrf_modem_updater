"""AT command batch result parser

This module turns the raw dict you get back from `RttTerminal.batch_at_query()`
into a readable PASS/FAIL console report.  It focuses on the commands you
listed, but is easy to extend – just add a new entry to `PARSERS`.

Usage example
-------------
```python
from at_parser import generate_report
report = term.batch_at_query(...)
print(generate_report(report))
```
The printed report looks like:
```
PASS  Modem functional            (no reply)
FAIL  Network registration        searching
PASS  Battery voltage             5.05 V
PASS  Modem temperature           25 °C
PASS  Network monitor             registered, LTE band 20, RSRP -87 dBm
```
PASS/FAIL is colour‑coded (green/red) when ANSI escape sequences are supported.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple

# ────────────────────────────────────────────────────────────
# Console colours
# ────────────────────────────────────────────────────────────
GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"


def _color(text: str, ok: bool, highlight: bool) -> str:  # internal helper
    if not highlight:
        return text
    return f"{GREEN if ok else RED}{text}{RESET}"


# ────────────────────────────────────────────────────────────
# Result container
# ────────────────────────────────────────────────────────────
@dataclass
class Parsed:
    value: Any               # machine‑readable value (int, dict, str…)
    description: str         # human‑readable summary


@dataclass
class TestResult:
    command: str
    name: str
    parsed: Parsed | None
    status: str | None
    passed: bool

    def line(self, highlight: bool = True) -> str:
        status_txt = "PASS" if self.passed else "FAIL"
        status_txt = _color(status_txt, self.passed, highlight)
        desc = self.parsed.description if self.parsed else "(no details)"
        return f"{status_txt:5}  {self.name:<25}  {desc}"


# ────────────────────────────────────────────────────────────
# Per‑command parser helpers
# ────────────────────────────────────────────────────────────

def _pass_if_ok(reply: str, status: str | None) -> Tuple[Parsed | None, bool, str]:
    passed = (status or "OK") == "OK"
    return Parsed(reply, reply or "(no reply)"), passed, "Modem functional"


def _parse_cereg(reply: str, _status: str | None) -> Tuple[Parsed | None, bool, str]:
    # +CEREG: <n>,<stat>,...
    m = re.match(r"\+CEREG: (\d),(\d)", reply)
    if not m:
        return Parsed(reply, "unparseable"), False, "Network registration"
    _n, stat = map(int, m.groups())
    meaning = {
        0: "not registered",
        1: "registered – home",
        2: "searching",
        3: "denied",
        4: "unknown",
        5: "registered – roaming",
    }.get(stat, "unknown")
    passed = stat in (1, 5)
    return Parsed(stat, meaning), passed, "Network registration"


def _simple_value(name: str) -> Callable[[str, str | None], Tuple[Parsed, bool, str]]:
    def parser(reply: str, status: str | None) -> Tuple[Parsed, bool, str]:
        ok = (status or "OK") == "OK" and bool(reply.strip())
        return Parsed(reply, reply), ok, name

    return parser


def _parse_xvbat(reply: str, _status: str | None) -> Tuple[Parsed, bool, str]:
    m = re.match(r"%XVBAT: (\d+)", reply)
    if not m:
        return Parsed(reply, "unparseable"), False, "Battery voltage"
    mv = int(m.group(1))
    description = f"{mv/1000:.2f} V"
    passed = 3_300 <= mv <= 5_500
    return Parsed(mv, description), passed, "Battery voltage"


def _parse_xtemp(reply: str, _status: str | None) -> Tuple[Parsed, bool, str]:
    m = re.match(r"%XTEMP: (-?\d+)", reply)
    if not m:
        return Parsed(reply, "unparseable"), False, "Modem temperature"
    temp = int(m.group(1))
    passed = -40 <= temp <= 85  # nRF91 operating range
    return Parsed(temp, f"{temp} °C"), passed, "Modem temperature"


def _parse_xsystemmode(reply: str, _status: str | None) -> Tuple[Parsed, bool, str]:
    m = re.match(r"%XSYSTEMMODE: (\d),(\d),(\d),(\d)", reply)
    if not m:
        return Parsed(reply, "unparseable"), False, "System mode"
    lte_m, nb_iot, gnss, pref = map(int, m.groups())
    modes: List[str] = []
    if lte_m:
        modes.append("LTE‑M")
    if nb_iot:
        modes.append("NB‑IoT")
    if gnss:
        modes.append("GNSS")
    description = ", ".join(modes) or "(none)"
    passed = lte_m == 1  # tweak to your needs
    return Parsed((lte_m, nb_iot, gnss, pref), description), passed, "System mode"


# ────────────────────────────────────────────────────────────
# NEW: full parser for %XMONITOR
# ────────────────────────────────────────────────────────────

def _parse_xmonitor(reply: str, _status: str | None) -> Tuple[Parsed, bool, str]:
    """Parse Nordic *%XMONITOR* readout.

    Example input (single line)::
        %XMONITOR: 1,"","","24201","81AE",7,20,"0331C805",281,6400,53,42,"","00100001","00000110","01011111"

    Returns a *Parsed* object whose *value* is a dict with the individual
    fields; *description* is a concise summary; PASS if registered (stat 1 or
    5) **and** RSRP stronger than ‑110 dBm.
    """
    # Strip prefix and feed to CSV so quoted commas are handled correctly
    prefix = "%XMONITOR: "
    if not reply.startswith(prefix):
        return Parsed(reply, "unparseable"), False, "Network monitor"

    csv_part = reply[len(prefix):].strip()
    try:
        fields = next(csv.reader([csv_part]))
    except Exception:
        return Parsed(reply, "unparseable"), False, "Network monitor"

    # Pad missing trailing fields with ""
    while len(fields) < 16:
        fields.append("")

    (
        reg_status,
        full_name,
        short_name,
        plmn,
        tac,
        act,
        band,
        cell_id,
        phys_cell_id,
        earfcn,
        rsrp_idx,
        snr_idx,
        *rest,
    ) = fields  # type: ignore[misc]

    # Convert numeric strings when present
    def _to_int(s: str) -> int | None:
        return int(s) if s and s.isdigit() else None

    reg_status_i = _to_int(reg_status) or -1
    band_i = _to_int(band)
    rsrp_i = _to_int(rsrp_idx)
    snr_i = _to_int(snr_idx)

    # Decode registration status to text
    status_meaning = {
        0: "not registered",
        1: "registered – home",
        2: "searching",
        3: "denied",
        4: "unknown",
        5: "registered – roaming",
    }.get(reg_status_i, "unknown")

    # Convert RSRP index → dBm per 3GPP TS 36.133 §9.1.4
    rsrp_dbm: int | None = None
    if rsrp_i is not None:
        rsrp_dbm = rsrp_i - 140  # index 0 == -140 dBm, 97 == -43 dBm

    # Convert SNR index (0.1 dB units) → dB
    snr_db: float | None = None
    if snr_i is not None:
        snr_db = snr_i / 10.0

    summary_parts: List[str] = [status_meaning]
    if band_i is not None:
        summary_parts.append(f"LTE band {band_i}")
    if rsrp_dbm is not None:
        summary_parts.append(f"RSRP {rsrp_dbm} dBm")
    if snr_db is not None:
        summary_parts.append(f"SNR {snr_db:.1f} dB")
    description = ", ".join(summary_parts)

    passed = reg_status_i in (1, 5) and (rsrp_dbm > -100)

    parsed_value = {
        "reg_status": reg_status_i,
        "plmn": plmn.strip('"'),
        "tac": tac.strip('"'),
        "act": _to_int(act),
        "band": band_i,
        "cell_id": cell_id.strip('"'),
        "phys_cell_id": _to_int(phys_cell_id),
        "earfcn": _to_int(earfcn),
        "rsrp_dbm": rsrp_dbm,
        "snr_db": snr_db,
    }

    return Parsed(parsed_value, description), passed, "Network monitor"


# ────────────────────────────────────────────────────────────
# Registry
# ────────────────────────────────────────────────────────────
PARSERS: Dict[str, Callable[[str, str | None], Tuple[Parsed | None, bool, str]]] = {
    "AT+CFUN=1": _pass_if_ok,
    "AT+CEREG?": _parse_cereg,
    "AT+CGMI": _simple_value("Manufacturer"),
    "AT+CGMR": _simple_value("Firmware version"),
    "AT+CGMM": _simple_value("Model"),
    "AT+CGSN": _simple_value("IMEI"),
    "AT+CIMI": _simple_value("IMSI"),
    "AT%XICCID": _simple_value("ICCID"),
    "AT%XMONITOR": _parse_xmonitor,  # updated!
    "AT%XVBAT": _parse_xvbat,
    "AT%XTEMP?": _parse_xtemp,
    "AT%XSYSTEMMODE?": _parse_xsystemmode,
    "AT+CFUN=0": _pass_if_ok,
}


# ────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────

def parse(report: Dict[str, Dict[str, str]], highlight: bool = True) -> List[TestResult]:
    """Return a list of *TestResult* objects."""
    results: List[TestResult] = []
    for cmd, info in report.items():
        reply = info.get("reply", "")
        status = info.get("status")
        parser = PARSERS.get(cmd, _default_parser)
        parsed, passed, name = parser(reply, status)
        results.append(TestResult(cmd, name, parsed, status, passed))
    return results


def generate_report(report: Dict[str, Dict[str, str]], highlight: bool = True) -> str:
    """Return a multi‑line string with nicely formatted results."""
    return "\n".join(r.line(highlight) for r in parse(report, highlight))


# ────────────────────────────────────────────────────────────
# Fallback for unknown commands
# ────────────────────────────────────────────────────────────

def _default_parser(reply: str, status: str | None) -> Tuple[Parsed | None, bool, str]:
    passed = (status or "OK") == "OK"
    return Parsed(reply, reply or "(no reply)"), passed, "Command"




# If executed as a script, run a quick demo with the sample data
def _demo() -> None:
    import json, textwrap

    sample = {
            "AT+CFUN=1": {
                "reply": "",
                "status": "OK"
            },
            "AT+CEREG?": {
                "reply": "+CEREG: 0,1,\"81AE\",\"0331C805\",7",
                "status": "OK"
            },
            "AT+CGMI": {
                "reply": "Nordic Semiconductor ASA",
                "status": "OK"
            },
            "AT+CGMR": {
                "reply": "mfw_nrf9160_1.3.7",
                "status": "OK"
            },
            "AT+CGMM": {
                "reply": "nRF9160-SICA",
                "status": "OK"
            },
            "AT+CGSN": {
                "reply": "350457791624248",
                "status": "OK"
            },
            "AT+CIMI": {
                "reply": "242016001128485",
                "status": "OK"
            },
            "AT%XICCID": {
                "reply": "%XICCID: 89470060210108095010",
                "status": "OK"
            },
            "AT%XMONITOR": {
                "reply": "%XMONITOR: 1,\"\",\"\",\"24201\",\"81AE\",7,20,\"0331C805\",281,6400,53,42,\"\",\"00100001\",\"00000110\",\"01011111\"",
                "status": "OK"
            },
            "AT%XVBAT": {
                "reply": "%XVBAT: 5046",
                "status": "OK"
            },
            "AT%XTEMP?": {
                "reply": "%XTEMP: 25",
                "status": "OK"
            },
            "AT%XSYSTEMMODE?": {
                "reply": "%XSYSTEMMODE: 1,1,1,0",
                "status": "OK"
            },
            "AT+CFUN=0": {
                "reply": "",
                "status": "OK"
            }
            }
    print(textwrap.indent(json.dumps(sample, indent=2), "  "))
    print("\nReport:\n")
    print(generate_report(sample))


if __name__ == "__main__":
    _demo()
