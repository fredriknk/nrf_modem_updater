#!/usr/bin/env python3
"""
flash_and_modem_update.py – Step 1 of your workflow
  • updates the nRF9160 modem to mfw_nrf9160_1.3.7.zip
  • flashes fw/nrf_rtt_at_client (ELF produced by cargo build --release)
"""
import time

from pathlib import Path
from pyocd.core.helpers import ConnectHelper
from pyocd.target.family.target_nRF91 import ModemUpdater
from pyocd.flash.file_programmer import FileProgrammer
from pyocd.target.family.target_nRF91 import ModemUpdater, exceptions
from rtt_terminal import RttTerminal
from at_parser import generate_report


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
        prog = FileProgrammer(
            session,
            smart_flash=True,      # skip pages that already match
            trust_crc=True,        # use nRF91’s on-chip page CRC to compare fast
        )
        prog.program(str(RTT_AT_CLIENT_ELF),file_format="elf")                                 # ²
        print("✓ Application flashed successfully")

        # -------- 4. reset & run so RTT starts -----------------------------
        session.target.reset_and_halt()
        session.target.resume()

        print("✓ Modem OK, AT-Client running – ready for RTT at client commands")
        #Run the interactive RTT terminal with theese commands and save the output for sending to a db
        at_commands_to_test = [
            "AT+CFUN=1", # Set the modem to full functionality mode +CFUN=<fun>
            "AT+CEREG?", # Network registration status reply: +CEREG: <n>,<stat>[,[<tac>],[<ci>],[<AcT>][,<cause_type>],[<reject_cause>][,[<Active-Time>],[<Periodic-TAU-ext>]]]]
            "AT+CGMI", # Manufacturer identification Nordic Semiconductor ASA <manufacturer>
            "AT+CGMR",# Firmware version nRF9160 SICA 1.3.7 to be sent to database <revision>
            "AT+CGMM", # Model identification ex nRF9160-SICA to be sent to database <model>
            "AT+CGSN", # Imei number ex 123456789012345 to be sent to database <IMEI>
            "AT+CIMI", # IMSI number ex 123456789012345 to be sent to database <IMSI>
            "AT%XICCID", # ICCID number ex 12345678901234567890 %XICCID: <ICCID>
            "AT%XMONITOR", # The proprietary %XMONITOR command reads a set of modem parameters reply: %XMONITOR: <reg_status>,[<full_name>,<short_name>,<plmn>,<tac>,<AcT>,<band>,<cell_id>, <phys_cell_id>,<EARFCN>,<rsrp>,<snr>,<NW-provided_eDRX_value>,<Active-Time>,<Periodic-TAU-ext>,<Periodic-TAU>]
            "AT%XVBAT", #Input voltage reply: +XVBAT: <vbat>
            "AT%XTEMP?", #Temperature reply: %XTEMP: <temperature>
            "AT%XSYSTEMMODE?", #System mode reply %XSYSTEMMODE: <LTE_M_support>,<NB_IoT_support>,<GNSS_support>,<LTE_preference>
            "AT+CFUN=0", # Set the modem to minimum functionality mode
        ]
        limits = {
            "System Voltage": {"min": 3700, "max": 4500},          # one rule
            "Modem temperature": {"max": 30},                      # one rule
            "Network monitor":      [                            # multiple rules -> AND
                {"field": "rsrp_dbm", "min": -95},
                {"field": "snr_db", "min": 15},
                {"field": "reg_status", "equals": 1},
            ],
            "Network registration": {"equals": 1},
            "Manufacturer": {"equals": "Nordic Semiconductor ASA"},
            "Firmware version": {"equals": "mfw_nrf9160_1.3.7"},
            "Model": {"equals": "nRF9160-SICA"},
        }
        print("Starting RTT terminal for AT commands... may take a few secs to start")
        term = RttTerminal(session=session, attach_console=False)
        term.start()              
        term.send("AT+CFUN=1")  # Set the modem to full functionality mode
        time.sleep(3)

        result = term.batch_at_query(at_commands_to_test,
                                    progress=lambda c: print("→", c),dwell=2, timeout=2.0)
        term.stop()
        
        txt, js = generate_report(result, limits, return_json=True, highlight=True)
        print(txt)


if __name__ == "__main__":
    main()