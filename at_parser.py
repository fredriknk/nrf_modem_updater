"""
at_parser.py – turn batch_at_query() results into a colour-coded PASS/FAIL
report, with optional per-test limits you control at runtime.

Example
-------
from at_parser import generate_report

raw = term.batch_at_query(at_commands)

limits = {
    "System Voltage": {"min": 3600, "max": 4500},      # mV
    "Modem temperature": {"max": 70},                   # °C
    "Network monitor": {"field": "rsrp_dbm", "min": -105},
}

print(generate_report(raw, limits))
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple

# ────────────────────────── ANSI helpers ──────────────────────────
GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"


def _color(txt: str, ok: bool, hilite: bool) -> str:
    return f"{GREEN if ok else RED}{txt}{RESET}" if hilite else txt
def _color_desc(desc: str, passed: bool, highlight: bool) -> str:
    return _color(desc, passed, highlight) if not passed else desc

# ────────────────────────── data containers ──────────────────────
@dataclass
class Parsed:
    value: Any
    description: str


@dataclass
class TestResult:
    command: str
    name: str
    parsed: Parsed | None
    status: str | None
    passed: bool
    reasons: list[str]    

    def line(self, highlight: bool = True) -> str:
        tag   = _color("PASS" if self.passed else "FAIL", self.passed, highlight)
        desc  = self.parsed.description if self.parsed else "(no details)"
        if not self.passed and self.reasons:
            desc = f"{desc}  [fail: {'; '.join(self.reasons)}]"
        desc  = _color_desc(desc, self.passed, highlight)
        return f"{tag:5}  {self.name:<25}  {desc}"
    # 1. helper: paint only if this field failed
    def _maybe_red(text: str, failed: bool, highlight: bool) -> str:
        return _color(text, False, highlight) if failed and highlight else text

    # 2. helper: did a rule for this field fail?
    def _field_failed(field: str, fail_set: set[str]) -> bool:
        return field in fail_set


# ────────────────────────── limit overrides ──────────────────────
def _apply_override(
    name: str,
    val: Any,
    default_pass: bool,
    limits: Dict[str, dict | list[dict]] | None,
) -> tuple[bool, list[str]]:
    """
    Evaluate user limits.
    Returns (overall_pass, reasons).  reasons = list of strings for rules that failed.
    """
    if not limits or name not in limits:
        return default_pass, []

    rules = limits[name]
    if not isinstance(rules, list):
        rules = [rules]

    reasons: list[str] = []

    def check(rule: dict) -> bool:
        v = val
        field = rule.get("field")
        if isinstance(v, dict) and field:
            v = v.get(field)
        if v is None:
            reasons.append(f"{field or 'value'} missing")
            return False

        if "equals" in rule:
            ok = v == rule["equals"]
            if not ok:
                reasons.append(f"{field or 'value'} ≠ {rule['equals']!r} (got {v!r})")
            return ok

        if "allowed" in rule:
            ok = v in rule["allowed"]
            if not ok:
                reasons.append(f"{v!r} not in {rule['allowed']}")
            return ok

        lo = rule.get("min", float("-inf"))
        hi = rule.get("max", float("inf"))
        try:
            ok = lo <= v <= hi  # type: ignore[operator]
        except TypeError:
            ok = False
        if not ok:
            bound = "<" if v < lo else ">" if v > hi else "!"
            reasons.append(f"{field or 'value'} {v} {bound} [{lo}‒{hi}]")
        return ok

    overall = all(check(r) for r in rules)
    return overall, reasons



# ────────────────────────── individual parsers ───────────────────
def _pass_if_ok(reply: str, status: str | None) -> Tuple[Parsed, bool]:
    return Parsed(reply, reply or "(no reply)"), (status or "OK") == "OK"


def _parse_cereg(reply: str, _s: str | None) -> Tuple[Parsed, bool]:
    m = re.match(r"\+CEREG: \d,(\d)", reply)
    if not m:
        return Parsed(reply, "unparseable"), False
    stat = int(m.group(1))
    desc = {
        0: "not registered",
        1: "registered – home",
        2: "searching",
        3: "denied",
        4: "unknown",
        5: "registered – roaming",
    }.get(stat, "unknown")
    return Parsed(stat, desc), stat in (1, 5)


def _simple_value(_label: str) -> Callable[[str, str | None], Tuple[Parsed, bool]]:
    def p(reply: str, status: str | None) -> Tuple[Parsed, bool]:
        ok = (status or "OK") == "OK" and bool(reply.strip())
        return Parsed(reply, reply), ok

    return p
def _simple_value_strip(_label: str) -> Callable[[str, str | None], Tuple[Parsed, bool]]:
    """
    Strip the AT prefix (everything up to & incl. the first ‘:’) so the
    description shows only the payload, e.g.
        "%XICCID: 8947…"  ->  "8947…"
    """
    def p(reply: str, status: str | None) -> Tuple[Parsed, bool]:
        ok = (status or "OK") == "OK" and bool(reply.strip())
        value = reply.split(":", 1)[-1].strip()      # keep whole string if no colon
        return Parsed(value, value), ok
    return p

def _parse_xvbat(reply: str, _s: str | None) -> Tuple[Parsed, bool]:
    m = re.match(r"%XVBAT: (\d+)", reply)
    if not m:
        return Parsed(reply, "unparseable"), False
    mv = int(m.group(1))
    return Parsed(mv, f"{mv/1000:.2f} V"), 3300 <= mv <= 5500


def _parse_xtemp(reply: str, _s: str | None) -> Tuple[Parsed, bool]:
    m = re.match(r"%XTEMP: (-?\d+)", reply)
    if not m:
        return Parsed(reply, "unparseable"), False
    t = int(m.group(1))
    return Parsed(t, f"{t} °C"), -40 <= t <= 85


def _parse_xsystemmode(reply: str, _s: str | None) -> Tuple[Parsed, bool]:
    m = re.match(r"%XSYSTEMMODE: (\d),(\d),(\d),(\d)", reply)
    if not m:
        return Parsed(reply, "unparseable"), False
    lte, nbiot, gnss, pref = map(int, m.groups())
    modes = [n for b, n in ((lte, "LTE-M"), (nbiot, "NB-IoT"), (gnss, "GNSS")) if b]
    return Parsed((lte, nbiot, gnss, pref), ", ".join(modes) or "(none)"), lte == 1


def _parse_xmonitor(reply: str, _s: str | None) -> Tuple[Parsed, bool]:
    if not reply.startswith("%XMONITOR: "):
        return Parsed(reply, "unparseable"), False
    row = next(csv.reader([reply[len("%XMONITOR: ") :]]))
    row += [""] * (16 - len(row))  # pad
    (
        reg,
        _f,
        _s,
        plmn,
        tac,
        act,
        band,
        cid,
        pcid,
        earfcn,
        rsrp_i,
        snr_i,
        *_,
    ) = row

    to_int = lambda x: int(x) if x.isdigit() else None
    reg_i, band_i = to_int(reg) or -1, to_int(band)
    rsrp_dbm = (to_int(rsrp_i) - 140) if to_int(rsrp_i) is not None else None
    snr_db = (to_int(snr_i) - 24 ) if to_int(snr_i) is not None else None
    status = {
        0: "not registered",
        1: "registered – home",
        2: "searching",
        3: "denied",
        4: "unknown",
        5: "registered – roaming",
    }.get(reg_i, "unknown")

    parts = [status]
    if band_i is not None:
        parts.append(f"LTE band {band_i}")
    if rsrp_dbm is not None:
        parts.append(f"RSRP {rsrp_dbm} dBm")
    if snr_db is not None:
        parts.append(f"SNR {snr_db:.1f} dB")

    parsed_val = dict(
        reg_status=reg_i,
        plmn=plmn.strip('"'),
        tac=tac.strip('"'),
        act=to_int(act),
        band=band_i,
        cell_id=cid.strip('"'),
        phys_cell_id=to_int(pcid),
        earfcn=to_int(earfcn),
        rsrp_dbm=rsrp_dbm,
        snr_db=snr_db,
    )
    default_pass = reg_i in (1, 5) and (rsrp_dbm is None or rsrp_dbm > -110)
    return Parsed(parsed_val, ", ".join(parts)), default_pass


# ────────────────────────── registry & names ────────────────────
PARSERS: Dict[str, Callable[[str, str | None], Tuple[Parsed, bool]]] = {
    "AT+CFUN=1": _pass_if_ok,
    "AT+CFUN=0": _pass_if_ok,
    "AT+CEREG?": _parse_cereg,
    "AT+CGMI": _simple_value("Manufacturer"),
    "AT+CGMR": _simple_value("Firmware"),
    "AT+CGMM": _simple_value("Model"),
    "AT+CGSN": _simple_value("IMEI"),
    "AT+CIMI": _simple_value("IMSI"),
    "AT%XICCID": _simple_value_strip("ICCID"),
    "AT%XMONITOR": _parse_xmonitor,
    "AT%XVBAT": _parse_xvbat,
    "AT%XTEMP?": _parse_xtemp,
    "AT%XSYSTEMMODE?": _parse_xsystemmode,
}

NAMES = {
    "AT+CFUN=1": "Modem functional",
    "AT+CFUN=0": "Modem functional",
    "AT+CEREG?": "Network registration",
    "AT+CGMI": "Manufacturer",
    "AT+CGMR": "Firmware version",
    "AT+CGMM": "Model",
    "AT+CGSN": "IMEI",
    "AT+CIMI": "IMSI",
    "AT%XICCID": "ICCID",
    "AT%XMONITOR": "Network monitor",
    "AT%XVBAT": "System Voltage",
    "AT%XTEMP?": "Modem temperature",
    "AT%XSYSTEMMODE?": "System mode",
}

# ────────────────────────── public API ──────────────────────────
def parse(
    report: Dict[str, Dict[str, str]],
    limits: Dict[str, dict] | None = None,
    highlight: bool = True,
) -> List[TestResult]:
    out: List[TestResult] = []
    for cmd, info in report.items():
        reply, status = info.get("reply", ""), info.get("status")
        parsed, default_pass = PARSERS.get(cmd, _pass_if_ok)(reply, status)
        passed, why = _apply_override(NAMES.get(cmd, "Command"), parsed.value, default_pass, limits)
        out.append(TestResult(cmd, NAMES.get(cmd, "Command"), parsed, status, passed, why))
    return out


def generate_report(
    report: Dict[str, Dict[str, str]],
    limits: Dict[str, dict] | None = None,
    highlight: bool = True,
) -> str:
    return "\n".join(r.line(highlight) for r in parse(report, limits, highlight))


# ────────────────────────── CLI demo ────────────────────────────
if __name__ == "__main__":
    demo = {
        "AT+CFUN=1": {"reply": "", "status": "OK"},
        "AT+CGMI": {"reply": "Nordic Semiconductor ASA", "status": "OK"},
        "AT+CGMR": {"reply": "nRF9160 SICA 1.3.7", "status": "OK"},
        "AT+CEREG?": {"reply": "+CEREG: 0,1,\"81AE\",\"0331C805\",7", "status": "OK"},
        "AT%XMONITOR": {
            "reply": "%XMONITOR: 1,\"\",\"\",\"24201\",\"81AE\",7,20,\"0331C805\",281,6400,30,42,\"\",\"00100001\",\"00000110\",\"01011111\"",
            "status": "OK",
        },
        "AT%XVBAT": {"reply": "%XVBAT: 5046", "status": "OK"},
        "AT%XTEMP?": {"reply": "%XTEMP: 25", "status": "OK"},
    }

    limits = {
        "System Voltage": {"min": 4900, "max": 5100},          # one rule
        "Modem temperature": {"max": 30},                      # one rule
        "Network monitor":      [                            # multiple rules -> AND
            {"field": "rsrp_dbm", "min": -105},
            {"field": "snr_db", "min": 10},
            {"field": "reg_status", "equals": 1},
        ],
        "Network registration": {"equals": 1},
        "Manufacturer": {"equals": "Nordic Semiconductor ASA"},
        "Firmware version": {"equals": "nRF9160 SICA 1.3.7"},
    }

    print(generate_report(demo,limits=limits, highlight=True))
