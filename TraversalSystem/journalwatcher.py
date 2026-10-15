import json
import os
import threading

# GuiFocus values from Status.json (Elite Dangerous journal manual)
GUI_NO_FOCUS = 0
GUI_INTERNAL_PANEL = 1   # right-hand panel
GUI_EXTERNAL_PANEL = 2   # left-hand panel
GUI_COMMS_PANEL = 3
GUI_ROLE_PANEL = 4
GUI_STATION_SERVICES = 5

# Status.json "Flags" bit 0 = Docked
FLAG_DOCKED = 1

# Journal events that should never happen while CATS is only supposed to be shuffling tritium around a docked
# carrier. If one shows up during a refuel window, something went wrong (menu misfire, Auto Launch, Disembark,
# an Operation launch pulling the commander onto an Operation Runner, ...) and we stop pressing keys immediately.
UNSAFE_EVENTS = {
    "Undocked",
    "Embark",
    "Disembark",
    "LaunchSRV",
    "LaunchFighter",
    "StartJump",
    "SupercruiseEntry",
    "Location",
    "LoadGame",
    "Shutdown",
    "Died",
}


def _is_unsafe_event(name: str) -> bool:
    # Catch-all for any Operations-related journal event Frontier may add. The exact event names aren't
    # documented, so this is a best-effort guess on top of the explicit list above.
    return name in UNSAFE_EVENTS or "operation" in name.lower()


class JournalWatcher:
    __slots__ = ["firstRun", "lastJournalText", "lastCarrierRequest", "hasJumped", "departureTime", "lastFuel",
                 "lastUsedFileName", "unsafeEvents", "lock", "statusFile"]

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.statusFile = ""
        self.lastUsedFileName = ""
        self.reset_all()

    def reset_all(self) -> None:
        with self.lock:
            self.firstRun = True
            self.lastJournalText = ""
            self.lastCarrierRequest = ""
            self.hasJumped = False
            self.departureTime = ""
            self.lastFuel = 1000
            self.unsafeEvents = []  # list of (sequence number, event name)

    def set_status_file(self, path: str) -> None:
        self.statusFile = path

    def process_journal(self, file_name) -> bool:
        with self.lock:
            self.lastUsedFileName = file_name

            with open(file_name, "r", encoding="utf-8") as journal:
                journalText = journal.read()

            if not self.firstRun and journalText != self.lastJournalText:
                if journalText.startswith(self.lastJournalText):
                    newText = journalText[len(self.lastJournalText):]
                else:
                    # Journal was rewritten/rotated, fall back to the old behaviour
                    newText = journalText.replace(self.lastJournalText, "")

                # Only consume complete lines. The game may be halfway through writing the last one.
                lastNewline = newText.rfind("\n")
                if lastNewline == -1:
                    consumed = ""
                else:
                    consumed = newText[:lastNewline + 1]

                for line in consumed.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._handle_event(event)

                if journalText.startswith(self.lastJournalText):
                    self.lastJournalText = self.lastJournalText + consumed
                else:
                    self.lastJournalText = journalText

            if self.firstRun:
                self.lastJournalText = journalText
            self.firstRun = False
            return True

    def _handle_event(self, event) -> None:
        name = event.get('event', '')

        if _is_unsafe_event(name):
            self.unsafeEvents.append((len(self.unsafeEvents) + 1, name))

        if name == "CarrierJumpRequest":
            destination = event['SystemName']
            self.lastCarrierRequest = destination
            print("Carrier destination: " + destination)
            self.departureTime = event['DepartureTime']
            print("Departure time: " + self.departureTime)

        elif name == "CarrierStats":
            fuel = event['FuelLevel']
            print("Fuel: " + str(fuel))

            if fuel < self.lastFuel and fuel < 100:
                print("alert:Your Tritium is running low.")

            self.lastFuel = fuel

        elif name == "CarrierJump":
            self.hasJumped = True

        elif name == "CargoTransfer":
            print("Cargo transfer logged: " + json.dumps(event.get('Transfers', [])))

    # ---- Tripwire -------------------------------------------------------------------------------------------

    def _refresh(self) -> None:
        if self.lastUsedFileName:
            self.process_journal(self.lastUsedFileName)

    def mark(self) -> int:
        """Returns a marker; pass it to unsafe_since() later to see what happened in between."""
        self._refresh()
        with self.lock:
            return len(self.unsafeEvents)

    def unsafe_since(self, marker: int) -> list:
        self._refresh()
        with self.lock:
            return [name for _, name in self.unsafeEvents[marker:]]

    # ---- Status.json ----------------------------------------------------------------------------------------

    def read_status(self):
        """Returns the parsed Status.json, or None if it can't be read (game not running, file mid-write...)."""
        if not self.statusFile:
            return None
        try:
            with open(self.statusFile, "r", encoding="utf-8") as f:
                text = f.read()
            return json.loads(text) if text.strip() else None
        except (OSError, json.JSONDecodeError):
            return None

    def gui_focus(self):
        status = self.read_status()
        if status is None:
            return None
        return status.get("GuiFocus", GUI_NO_FOCUS)

    def is_docked(self):
        status = self.read_status()
        if status is None:
            return None
        return bool(status.get("Flags", 0) & FLAG_DOCKED)

    # ---- Existing API ---------------------------------------------------------------------------------------

    def last_carrier_request(self) -> str:
        self.process_journal(self.lastUsedFileName)
        return self.lastCarrierRequest

    def reset_jump(self) -> None:
        self.hasJumped = False

    def get_jumped(self) -> bool:
        self.process_journal(self.lastUsedFileName)
        return self.hasJumped
