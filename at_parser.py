"""
at_parser.py- Parse Nordic-style batch AT command replies.

Features
========
* Colour-coded PASS/FAIL console log (ANSI auto-disabled when piped).
* User-supplied limits- numeric ranges, equality, per-field, AND-combined.
* Structured JSON return for CI dashboards.
* CSV export helper for long-term plotting.
* Pluggable parsers- add a new command in one line.

Quick usage
-----------
from at_parser import generate_report, register_parser, export_csv

raw = term.batch_at_query(at_commands)
limits = {"System Voltage": {"min": 3600, "max": 4500}}

text, data = generate_report(raw, limits, return_json=True)
print(text)

export_csv("daily_log.csv", data)

# add a parser on-the-fly
register_parser(
    "AT+FWTEST?",
    lambda r, s: (Parsed(r, r), (s or "OK") == "OK"),
    "FW self-test"
)
"""
from __future__ import annotations

import csv as _csv
import json as _json
import re as _re
import sys as _sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

# ── ANSI helpers ──────────────────────────────────────────────────────────────
_GREEN, _RED, _RESET = "\033[92m", "\033[91m", "\033[0m"


def _tty_supports_color() -> bool:
    return _sys.stdout.isatty() and _sys.platform != "win32"


def _color(text: str, ok: bool, enable: bool) -> str:
    if not enable:
        return text
    return f"{_GREEN if ok else _RED}{text}{_RESET}"


def _color_if_fail(text: str, passed: bool, enable: bool) -> str:
    return _color(text, False, enable) if not passed else text


# ── data containers ───────────────────────────────────────────────────────────
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
    reasons: List[str]

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.parsed:
            d.update(value=self.parsed.value, description=self.parsed.description)
        d.pop("parsed", None)
        return d

    def line(self, color: bool = True) -> str:
        tag = _color("PASS" if self.passed else "FAIL", self.passed, color)
        desc = self.parsed.description if self.parsed else "(no details)"
        if not self.passed and self.reasons:
            desc += f"  [fail: {'; '.join(self.reasons)}]"
        desc = _color_if_fail(desc, self.passed, color)
        return f"{tag:5}  {self.name:<25}  {desc}"


# ── registry- lets callers add/override parsers ──────────────────────────────
PARSER_REGISTRY: Dict[str, Callable[[str, str | None], Tuple[Parsed, bool]]] = {}
NAME_REGISTRY: Dict[str, str] = {}


def register_parser(
    cmd: str,
    parser: Callable[[str, str | None], Tuple[Parsed, bool]],
    name: str,
    *,
    override: bool = False,
) -> None:
    if not override and cmd in PARSER_REGISTRY:
        raise ValueError(f"Parser for {cmd} already exists- use override=True")
    PARSER_REGISTRY[cmd] = parser
    NAME_REGISTRY[cmd] = name


# ── rule engine ───────────────────────────────────────────────────────────────
def _apply_rules(val: Any, rules: List[dict]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []

    def ok(rule: dict) -> bool:
        v = val
        field = rule.get("field")
        if isinstance(v, dict) and field:
            v = v.get(field)
        if v is None:
            reasons.append(f"{field or 'value'} missing")
            return False
        if "equals" in rule:
            passed = v == rule["equals"]
            if not passed:
                reasons.append(f"{field or 'value'} ≠ {rule['equals']!r} (got {v!r})")
            return passed
        if "allowed" in rule:
            passed = v in rule["allowed"]
            if not passed:
                reasons.append(f"{v!r} not in {rule['allowed']}")
            return passed
        lo = rule.get("min", float("-inf"))
        hi = rule.get("max", float("inf"))
        try:
            passed = lo <= v <= hi  # type: ignore[operator]
        except TypeError:
            passed = False
        if not passed:
            reasons.append(f"{field or 'value'} {v} outside [{lo}–{hi}]")
        return passed

    overall = all(ok(r) for r in rules)
    return overall, reasons


# ── built-in parsers (compact) ────────────────────────────────────────────────
_INT_RE = _re.compile(r"[-+]?\d+")


def _pass_if_ok(reply: str, status: str | None) -> Tuple[Parsed, bool]:
    return Parsed(reply, reply or "(no reply)"), (status or "OK") == "OK"


def _verbatim(label: str, strip_prefix=False):
    def fn(reply: str, status: str | None) -> Tuple[Parsed, bool]:
        if strip_prefix and ":" in reply:
            reply = reply.split(":", 1)[1].strip()
        ok = (status or "OK") == "OK" and bool(reply)
        return Parsed(reply, reply), ok

    return fn


def _parse_cereg(r: str, _s) -> Tuple[Parsed, bool]:
    m = _re.search(r"\+CEREG: \d,(\d)", r)
    if not m:
        return Parsed(r, "unparseable"), False
    stat = int(m.group(1))
    txt = {
        0: "not registered",
        1: "registered- home",
        2: "searching",
        3: "denied",
        4: "unknown",
        5: "registered- roaming",
    }.get(stat, "unknown")
    return Parsed(stat, txt), stat in (1, 5)


def _parse_xvbat(r: str, _):  # mV
    m = _re.search(r"%XVBAT:\s*(\d+)", r)
    if not m:
        return Parsed(r, "unparseable"), False
    mv = int(m.group(1))
    return Parsed(mv, f"{mv/1000:.2f} V"), 3300 <= mv <= 5500


def _parse_xtemp(r: str, _):
    m = _re.search(r"%XTEMP:\s*(-?\d+)", r)
    if not m:
        return Parsed(r, "unparseable"), False
    t = int(m.group(1))
    return Parsed(t, f"{t} °C"), -40 <= t <= 85


def _parse_xsystemmode(r: str, _):
    m = _re.search(r"%XSYSTEMMODE: (\d),(\d),(\d),(\d)", r)
    if not m:
        return Parsed(r, "unparseable"), False
    lte, nb, gnss, pref = map(int, m.groups())
    modes = [n for b, n in ((lte, "LTE-M"), (nb, "NB-IoT"), (gnss, "GNSS")) if b]
    return Parsed((lte, nb, gnss, pref), ", ".join(modes) or "(none)"), lte == 1


def _parse_xmonitor(reply: str, _s) -> Tuple[Parsed, bool]:
    """Parse Nordic proprietary %XMONITOR readout.

    Index map per v2.0 AT spec:
        0  reg_status
        1  full_name (MCC-MNC text)  - ignored
        2  short_name                - ignored
        3  plmn                      - ignored
        4  tac                       - ignored
        5  AcT                       - ignored
        6  LTE band
        7  cell_id                   - ignored
        8  phys_cell_id             - ignored
        9  EARFCN                   - ignored
       10  RSRP index (0-97)
       11  SNR  index (0-250)
    Remaining fields: eDRX/TAU etc.
    """
    prefix = "%XMONITOR: "
    if not reply.startswith(prefix):
        return Parsed(reply, "unparseable"), False

    row = next(_csv.reader([reply[len(prefix):]])) + [""] * 16

    # new
    reg     = row[0]   # <reg_status>
    band    = row[6]   # LTE band
    rsrp_i  = row[10]  # RSRP index
    snr_i   = row[11]  # SNR index
    to_int = lambda x: int(x) if _INT_RE.fullmatch(x or "") else None
    reg_i, band_i = to_int(reg) or -1, to_int(band)
    rsrp = (to_int(rsrp_i) - 140) if to_int(rsrp_i) is not None else None
    snr = (to_int(snr_i) - 24) if to_int(snr_i) is not None else None
    status = {
        0: "not registered",
        1: "registered- home",
        2: "searching",
        3: "denied",
        4: "unknown",
        5: "registered- roaming",
    }.get(reg_i, "unknown")
    parts = [status]
    if band_i is not None:
        parts.append(f"LTE band {band_i}")
    if rsrp is not None:
        parts.append(f"RSRP {rsrp} dBm")
    if snr is not None:
        parts.append(f"SNR {snr:.1f} dB")
    parsed = Parsed(
        {"reg_status": reg_i, "band": band_i, "rsrp_dbm": rsrp, "snr_db": snr},
        ", ".join(parts),
    )
    default_ok = reg_i in (1, 5) and (rsrp is None or rsrp > -110)
    return parsed, default_ok


# ── register built-ins ────────────────────────────────────────────────────────
register_parser("AT+CFUN=1", _pass_if_ok, "Modem functional", override=True)
register_parser("AT+CFUN=0", _pass_if_ok, "Modem functional", override=True)
register_parser("AT+CEREG?", _parse_cereg, "Network registration", override=True)
register_parser("AT+CGMI", _verbatim("Manufacturer"), "Manufacturer", override=True)
register_parser("AT+CGMR", _verbatim("Firmware"), "Firmware version", override=True)
register_parser("AT+CGMM", _verbatim("Model"), "Model", override=True)
register_parser("AT+CGSN", _verbatim("IMEI"), "IMEI", override=True)
register_parser("AT+CIMI", _verbatim("IMSI"), "IMSI", override=True)
register_parser("AT%XICCID", _verbatim("ICCID", strip_prefix=True), "ICCID", override=True)
register_parser("AT%XMONITOR", _parse_xmonitor, "Network monitor", override=True)
register_parser("AT%XVBAT", _parse_xvbat, "System Voltage", override=True)
register_parser("AT%XTEMP?", _parse_xtemp, "Modem temperature", override=True)
register_parser("AT%XSYSTEMMODE?", _parse_xsystemmode, "System mode", override=True)

# ── public helpers ────────────────────────────────────────────────────────────
def _results_to_csv_rows(results: List[TestResult]) -> Iterable[List[str]]:
    header = [
        "command",
        "name",
        "passed",
        "status",
        "description",
        "value",
        "reasons",
    ]
    yield header
    for r in results:
        d = r.as_dict()
        yield [
            r.command,
            r.name,
            "PASS" if r.passed else "FAIL",
            d.get("status") or "",
            d.get("description") or "",
            _json.dumps(d.get("value"), ensure_ascii=False),
            "; ".join(r.reasons),
        ]


def export_csv(path: str | Path, results: List[TestResult], mode: str = "a") -> None:
    """
    Append results to *path* (or create it).  The CSV always has a header row
    the first time it's written.
    """
    path = Path(path)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open(mode, newline="") as fp:
        w = _csv.writer(fp)
        if write_header:
            w.writerow(next(_results_to_csv_rows(results)))
        for row in _results_to_csv_rows(results):
            if row[0] == "command":  # skip header inside generator
                continue
            w.writerow(row)


def parse(
    report: Dict[str, Dict[str, str]],
    limits: Dict[str, dict | List[dict]] | None = None,
) -> List[TestResult]:
    out: List[TestResult] = []
    for cmd, info in report.items():
        reply, status = info.get("reply", ""), info.get("status")
        parser = PARSER_REGISTRY.get(cmd, _pass_if_ok)
        parsed, default_ok = parser(reply, status)
        rules = limits.get(NAME_REGISTRY.get(cmd, cmd), []) if limits else []
        if isinstance(rules, dict):
            rules = [rules]
        passed, why = _apply_rules(parsed.value, rules) if rules else (default_ok, [])
        passed = passed and default_ok if rules else default_ok
        out.append(
            TestResult(cmd, NAME_REGISTRY.get(cmd, cmd), parsed, status, passed, why)
        )
    return out


def generate_report(
    report: Dict[str, Dict[str, str]],
    limits: Dict[str, dict | List[dict]] | None = None,
    *,
    highlight: bool | None = None,
    return_json: bool = False,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Return (text_report, json_data).
    *highlight*=None → auto (on if tty).
    """
    highlight = _tty_supports_color() if highlight is None else highlight
    results = parse(report, limits)
    text = "\n".join(r.line(highlight) for r in results)
    data = [r.as_dict() for r in results]
    return (text, data) if return_json else (text, [])

# ────────────────────────── CLI demo ────────────────────────────
if __name__ == "__main__":
    sample = {
        "AT+CFUN=1": {"reply": "", "status": "OK"},
        "AT+CGMI": {"reply": "Nordic Semiconductor ASA", "status": "OK"},
        "AT+CGMR": {"reply": "nRF9160 SICA 1.3.7", "status": "OK"},
        "AT+CEREG?": {"reply": "+CEREG: 0,1,\"81AE\",\"0331C805\",7", "status": "OK"},
        "AT%XMONITOR": {
            "reply": "%XMONITOR: 1,\"\",\"\",\"24201\",\"81AE\",7,20,\"0331C805\",281,6400,47,42,\"\",\"00100001\",\"00000110\",\"01011111\"",
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
            #{"field": "reg_status", "equals": 1},
        ],
        "Network registration": {"equals": 1},
        "Manufacturer": {"equals": "Nordic Semiconductor ASA"},
        "Firmware version": {"equals": "nRF9160 SICA 1.3.7"},
    }

    txt, js = generate_report(sample, limits, return_json=True, highlight=True)
    print(txt)
