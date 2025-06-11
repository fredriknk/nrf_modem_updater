#!/usr/bin/env python3
"""
flash_and_modem_update.py – Step 1 of your workflow
  • updates the nRF9160 modem to mfw_nrf9160_1.3.7.zip
  • flashes fw/nrf_rtt_at_client (ELF produced by cargo build --release)
"""

from pathlib import Path
from pyocd.core.helpers import ConnectHelper
from pyocd.target.family.target_nRF91 import ModemUpdater
from pyocd.flash.file_programmer import FileProgrammer
from pyocd.target.family.target_nRF91 import ModemUpdater, exceptions


MODEM_ZIP   = Path("fw/mfw_nrf9160_1.3.7.zip")
RTT_AT_CLIENT_ELF     = Path("fw/nrf_rtt_at_client_npm1300")

def main():
    # -------- 1. open a pyOCD session on the first attached probe ----------
    with ConnectHelper.session_with_chosen_probe(options={"frequency": 4000000, "target_override": "nrf91"}) as session:
        board = session.board
        target = board.target
        
        # -------- 2. program + verify modem FW -----------------------------
        try:
            print(f"Verifyiing {MODEM_ZIP.name} …")
            ModemUpdater(session).verify(str(MODEM_ZIP)) 
            print("✓ Modem already up-to-date")
        except exceptions.TargetError as e:
            print(f"Modem verification failed: {e}")    
            print(f"Updating modem to {MODEM_ZIP.name} …")
            ModemUpdater(session).program_and_verify(str(MODEM_ZIP)) 
            print("✓ Modem updated successfully")

        # -------- 3. flash your RTT client ELF -----------------------------
        print(f"Flashing {RTT_AT_CLIENT_ELF.name} …")
        prog = FileProgrammer(session)
        prog.program(str(RTT_AT_CLIENT_ELF),file_format="elf")                                 # ²
        print("✓ Application flashed successfully")

        # -------- 4. reset & run so RTT starts -----------------------------
        session.target.reset_and_halt()
        session.target.resume()

        print("✓ Modem OK, AT-Client running – ready for RTT at client commands")

        at_commands_to_test = [
            "AT+CFUN=1", # Functionality test command
            "AT+CGMI", # Manufacturer identification Nordic Semiconductor ASA
            "AT+CGMR",# Firmware version nRF9160 SICA 1.3.7 to be sent to database
            "AT+CGMM", # Model identification ex nRF9160-SICA to be sent to database
            "AT+CGSN", # Imei number ex 123456789012345 to be sent to database
            "AT+CIMI", # IMSI number ex 123456789012345 to be sent to database
            "AT%XICCID", # ICCID number ex 12345678901234567890
            "AT%XMONITOR", # The proprietary %XMONITOR command reads a set of modem parameters reply: %XMONITOR: <reg_status>,[<full_name>,<short_name>,<plmn>,<tac>,<AcT>,<band>,<cell_id>, <phys_cell_id>,<EARFCN>,<rsrp>,<snr>,<NW-provided_eDRX_value>,<Active-Time>,<Periodic-TAU-ext>,<Periodic-TAU>]
            "AT+CEREG"
            "AT%XVBAT", #Input voltage
            "AT%XTEMP?", #Temperature reply:%XTEMP: <temperature>
            "AT%XSYSTEMMODE?", #System mode reply %XSYSTEMMODE: <LTE_M_support>,<NB_IoT_support>,<GNSS_support>,<LTE_preference>
        ]

if __name__ == "__main__":
    main()