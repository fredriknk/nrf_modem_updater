#!/usr/bin/env python3
"""
nrf_flash_tool.py – Swiss-army CLI for the nRF9160 workflow

Typical invocations
────────────────────────────────────────────────────────
# Just update the modem
python ./nrf_flash_tool.py --update-modem

# Update modem + flash RTT client
python ./nrf_flash_tool.py --update-modem --flash-rt-client

# Wrtite security certificates
python ./nrf_flash_tool.py --reboot --test-at --write-certs

# Flash AT Tool Test AT commands + write security certificates
python ./nrf_flash_tool.py --flash-rt-client --test-at --write-certs

# Full production test sequence
python ./nrf_flash_tool.py --update-modem --flash-rt-client --test-at --write-certs --flash-main --debug --reset-on-exit
"""

import argparse
import sys
import time
from pathlib import Path
from contextlib import contextmanager

from pyocd.core.helpers import ConnectHelper
from pyocd.target.family.target_nRF91 import ModemUpdater, exceptions
from pyocd.flash.file_programmer import FileProgrammer

import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

from rtt_terminal import RttTerminal

import subprocess, threading, queue

from at_parser import (
    generate_report,
    register_parser,
    _parse_cmng_read_sha,
    _pass_if_ok,
)
from at_cmng_builder import issue_with_ca


# ---- default artefacts ------------------------------------------------------
MODEM_ZIP_DEFAULT        = Path("fw/mfw_nrf9160_1.3.7.zip")
RTT_AT_CLIENT_ELF_DEFAULT = Path("fw/nrf_rtt_at_client_npm1300")
APPLICATION_ELF_DEFAULT  = Path("fw/msense-firmware")

AT_COMMANDS = [
    "AT+CFUN=1",
    "AT+CEREG?",
    "AT+CGMI",
    "AT+CGMR",
    "AT+CGMM",
    "AT+CGSN",
    "AT+CIMI",
    "AT%XICCID",
    "AT%XMONITOR",
    "AT%XVBAT",
    "AT%XTEMP?",
    "AT%XSYSTEMMODE?",
    "AT+CFUN=0",
]

AT_REPLY_LIMITS = {
    "System Voltage": {"min": 4900, "max": 5100},
    "Modem temperature": {"max": 30},
    "Network monitor": [
        {"field": "rsrp_dbm", "min": -106},
        {"field": "snr_db", "min": 15},
        {"field": "reg_status", "equals": 1},
    ],
    "Network registration": {"equals": 1},
    "Manufacturer": {"equals": "Nordic Semiconductor ASA"},
    "Firmware version": {"equals": "mfw_nrf9160_1.3.7"},
    "Model": {"equals": "nRF9160-SICA"},
    }


# ---- reusable helpers -------------------------------------------------------
@contextmanager
def open_session():
    """Context-manager that yields a connected pyOCD Session."""
    with ConnectHelper.session_with_chosen_probe(
        options={"frequency": 4_000_000, "target_override": "nrf91"}
    ) as session:
        yield session


def flash_elf(session, elf: Path, *, smart=True, verify_crc=True):
    """Flash an ELF with pyOCD’s FileProgrammer."""
    print(f"Flashing {elf.name} …")
    FileProgrammer(
        session,
        smart_flash=smart,
        trust_crc=verify_crc,
    ).program(str(elf), file_format="elf")
    print("✓ Flash done")

def stream_defmt(
    *,
    elf: str,
    chip: str = "nRF9160_xxAA",
    log_format: str | None = None,
    on_line: Optional[Callable[[str], None]] = None,
    reset_on_close: bool = True,      
) -> subprocess.Popen:
    """
    Spawn `probe-rs attach`, decode RTT/defmt, colour the output and forward it.

    Parameters
    ----------
    elf : str
        Path to the exact ELF that is already running on the MCU.
    chip : str, optional
        Probe-RS chip identifier (default ``"nRF9160_xxAA"``).
    log_format : str | None, optional
        defmt printer format string.  If *None*, a sensible coloured default is
        used (timestamp dimmed, level colour-coded & bold, then the message).
    on_line : Callable[[str], None] | None, optional
        Callback for each decoded log line.  Falls back to ``print``.
    """
    elf_path = Path(elf).expanduser()
    if not elf_path.exists():
        raise FileNotFoundError(elf_path)

    # default: grey timestamp · coloured/bold level · plain message
    if log_format is None:
        log_format = "{t:dimmed} [{L:severity:bold}] {s}"

    # --- build the probe-rs command -------------------------------------------
    cmd = [
        "probe-rs",
        "attach",
        "--chip", chip,
        str(elf_path),
        "--log-format", log_format,
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        text=True,     # UTF-8 lines out
        bufsize=1,     # line buffered
    )

    cb = on_line or print

    def _forward():
        try:
            for line in proc.stdout:
                cb(line.rstrip("\n"))
        finally:
            proc.stdout.close()

    threading.Thread(target=_forward, name="defmt-stream", daemon=True).start()

    return proc

def _reset_rtt_terminal():
    """Reset the RTT terminal on exit."""
    try:
        subprocess.run(["probe-rs", "reset", "--chip", "nRF9160_xxAA"], check=True)
        print("✓ RTT terminal reset")
    except subprocess.CalledProcessError as e:
        print(f"✗ Failed to reset RTT terminal: {e}")
        sys.exit(1)

# -----------------------------------------------------------------------------


def step_update_modem(session, modem_zip: Path):
    updater = ModemUpdater(session)
    print(f"updating modem FW → {modem_zip.name}")
    print("→ Programming modem …")
    try:
        session.target.reset_and_halt()
        updater.program_and_verify(str(modem_zip))
        print("✓ Modem updated")
    except exceptions.TargetError as err:
        print(f"✗ Modem update failed: {err}")
        sys.exit(1)
    


def step_flash_client(session, elf: Path):
    flash_elf(session, elf)
    session.target.reset_and_halt()
    session.target.resume()
    print("✓ RTT AT-client running")

def step_debug_defmt(session=None, elf = APPLICATION_ELF_DEFAULT):
    print("→ Opening RTT for debug …")
    if session != None:
        session.close()
    proc = stream_defmt(elf=elf)
    #exit on ctrl-c
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping defmt stream …")
        proc.terminate()
        proc.wait()
        print("✓ Defmt stream stopped")


def step_test_at(session):
    print("→ Opening RTT for AT test …")
    term = RttTerminal(session=session, attach_console=False)
    term.start()
    term.send("AT+CFUN=1")
    time.sleep(3)

    result = term.batch_at_query(
        AT_COMMANDS,
        progress=lambda c: print("  ↳", c),
        dwell=2,
        timeout=2.0,
    )
    term.stop()

    txt, _ = generate_report(result, AT_REPLY_LIMITS, return_json=True, highlight=True)
    print(txt)


def step_write_certs(session):
    sec_tag = 16842753

    # ── build AT command list + parsers/limits ────────────────────────────────
    cmds, pems = issue_with_ca(
        sec_tag=sec_tag,
        client_cn="msense",
        ca_crt_path="certs/ca.crt",
        ca_key_path="certs/ca.key",
        days=3650,
    )

    limits = {}
    for i, cmd in enumerate(cmds):
        register_parser(cmd, _pass_if_ok, f"Write cert sec_tag={sec_tag} pos={i}")
        limits[cmd] = {"equals": "OK"}

    sha_hashes = pems["sha"]
    for i in (0, 1, 2):
        read_cmd = f"AT%CMNG=1,{sec_tag},{i}"
        cmds.append(read_cmd)
        register_parser(read_cmd, _parse_cmng_read_sha, f"SHA cert {i}")
        limits[f"SHA cert {i}"] = {"equals": sha_hashes[i]}

    # ── execute over RTT ──────────────────────────────────────────────────────
    print("→ Opening RTT for certificate write …")
    term = RttTerminal(session=session, attach_console=False)
    term.start()
    term.send("AT+CFUN=0")        # modem off for CMNG writes
    time.sleep(3)

    result = term.batch_at_query(cmds, dwell=4, timeout=5.0)
    term.stop()

    txt, _ = generate_report(result, limits, return_json=True, highlight=True)
    print(txt)


def step_flash_main(session, elf: Path):
    # Placeholder – adapt as needed
    print("[placeholder] Flashing main program…")
    flash_elf(session, elf)


# -----------------------------------------------------------------------------


def build_cli(argv=None):
    p = argparse.ArgumentParser(
        description="All-in-one flashing & test tool for the nRF9160."
    )
    p.add_argument("--reboot", action="store_true",
                   help="Reboot the target (default: False)")
    
    p.add_argument("--update-modem", action="store_true",
                   help="Update modem FW to the given ZIP (default: 1.3.7)")
    p.add_argument("--modem-zip", type=Path, default=MODEM_ZIP_DEFAULT,
                   metavar="ZIP", help="Override modem ZIP path")

    p.add_argument("--flash-rt-client", action="store_true",
                   help="Flash RTT AT client ELF")
    
    p.add_argument("--rt-client-elf", type=Path, default=RTT_AT_CLIENT_ELF_DEFAULT,
                   metavar="ELF", help="Override RTT client ELF path")

    p.add_argument("--test-at", action="store_true",
                   help="Run automated AT command test suite")

    p.add_argument("--write-certs", action="store_true",
                   help="Generate & write security certificates")

    p.add_argument("--flash-main", action="store_true",
                   help="Flash client APP ELF")
    
    p.add_argument("--client-elf", type=Path, default=APPLICATION_ELF_DEFAULT,
                   metavar="ELF", help="Override client ELF path")
    
    p.add_argument("--debug", action="store_true",
                   help="Enable debug output (default: False)")
    
    p.add_argument("--reset-on-exit", action="store_true",
                   help="reset the RTT terminal on exit (default: True)")


    return p.parse_args(argv)


def main(argv=None):
    args = build_cli(argv)

    if not any(
        [   
            args.reboot,
            args.update_modem,
            args.flash_rt_client,
            args.test_at,
            args.write_certs,
            args.flash_main,
            args.debug,
            args.reset_on_exit
        ]
    ):
        print("Nothing to do – see -h for help")
        return

    with open_session() as session:

        if args.reboot:
            print("Rebooting target …")
            session.target.reset_and_halt()
            session.target.resume()
            print("✓ Target rebooted")

        if args.update_modem:
            step_update_modem(session, args.modem_zip)

        if args.flash_rt_client:
            step_flash_client(session, args.rt_client_elf)

        if args.test_at:
            step_test_at(session)

        if args.write_certs:
            step_write_certs(session)

        if args.flash_main:
            step_flash_client(session, args.client_elf)

        if args.debug:
            step_debug_defmt(session, args.client_elf)

        if args.reset_on_exit:
            _reset_rtt_terminal()

# -----------------------------------------------------------------------------


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted by user")
