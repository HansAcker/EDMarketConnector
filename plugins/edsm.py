#
# System display and EDSM lookup
#

# TODO:
#  1) Re-factor EDSM API calls out of journal_entry() into own function.
#  2) Fix how StartJump already changes things, but only partially.
#  3) Possibly this and other two 'provider' plugins could do with being
#    based on a single class that they extend.  There's a lot of duplicated
#    logic.
#  4) Ensure the EDSM API call(back) for setting the image at end of system
#    text is always fired.  i.e. CAPI cmdr_data() processing.

import json
import logging
import sys
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from queue import Queue
from threading import Thread
from typing import TYPE_CHECKING, Any, List, Mapping, MutableMapping, Optional, Tuple

import requests

import myNotebook as nb  # noqa: N813
import plug
from config import applongname, appname, appversion, config
from ttkHyperlinkLabel import HyperlinkLabel

if TYPE_CHECKING:
    def _(x: str) -> str:
        return x

logger = logging.getLogger(appname)

EDSM_POLL = 0.1
_TIMEOUT = 20


this: Any = sys.modules[__name__]  # For holding module globals
this.session: requests.Session = requests.Session()
this.queue: Queue = Queue()		# Items to be sent to EDSM by worker thread
this.discardedEvents: List[str] = []  # List discarded events from EDSM
this.lastlookup: bool = False		# whether the last lookup succeeded

# Game state
this.multicrew: bool = False  # don't send captain's ship info to EDSM while on a crew
this.coordinates: Optional[Tuple[int, int, int]] = None
this.newgame: bool = False  # starting up - batch initial burst of events
this.newgame_docked: bool = False  # starting up while docked
this.navbeaconscan: int = 0		# batch up burst of Scan events after NavBeaconScan
this.system_link: tk.Tk = None
this.system: tk.Tk = None
this.system_address: Optional[int] = None  # Frontier SystemAddress
this.system_population: Optional[int] = None
this.station_link: tk.Tk = None
this.station: Optional[str] = None
this.station_marketid: Optional[int] = None  # Frontier MarketID
STATION_UNDOCKED: str = '×'  # "Station" name to display when not docked = U+00D7
__cleanup = str.maketrans({' ': None, '\n': None})
IMG_KNOWN_B64 = """
R0lGODlhEAAQAMIEAFWjVVWkVWS/ZGfFZ////////////////yH5BAEKAAQALAAAAAAQABAAAAMvSLrc/lAFIUIkYOgNXt5g14Dk0AQlaC1CuglM6w7wgs7r
MpvNV4q932VSuRiPjQQAOw==
""".translate(__cleanup)

IMG_UNKNOWN_B64 = """
R0lGODlhEAAQAKEDAGVLJ+ddWO5fW////yH5BAEKAAMALAAAAAAQABAAAAItnI+pywYRQBtA2CtVvTwjDgrJFlreEJRXgKSqwB5keQ6vOKq1E+7IE5kIh4kC
ADs=
""".translate(__cleanup)

IMG_NEW_B64 = """
R0lGODlhEAAQAMZwANKVHtWcIteiHuiqLPCuHOS1MN22ZeW7ROG6Zuu9MOy+K/i8Kf/DAuvCVf/FAP3BNf/JCf/KAPHHSv7ESObHdv/MBv/GRv/LGP/QBPXO
PvjPQfjQSvbRSP/UGPLSae7Sfv/YNvLXgPbZhP7dU//iI//mAP/jH//kFv7fU//fV//ebv/iTf/iUv/kTf/iZ/vgiP/hc/vgjv/jbfriiPriiv7ka//if//j
d//sJP/oT//tHv/mZv/sLf/rRP/oYv/rUv/paP/mhv/sS//oc//lkf/mif/sUf/uPv/qcv/uTv/uUv/vUP/qhP/xP//pm//ua//sf//ubf/wXv/thv/tif/s
lv/tjf/smf/yYP/ulf/2R//2Sv/xkP/2av/0gP/ylf/2df/0i//0j//0lP/5cP/7a//1p//5gf/7ev/3o//2sf/5mP/6kv/2vP/3y//+jP//////////////
/////////////////////////////////////////////////yH5BAEKAH8ALAAAAAAQABAAAAePgH+Cg4SFhoJKPIeHYT+LhVppUTiPg2hrUkKPXWdlb2xH
Jk9jXoNJQDk9TVtkYCUkOy4wNjdGfy1UXGJYOksnPiwgFwwYg0NubWpmX1ArHREOFYUyWVNIVkxXQSoQhyMoNVUpRU5EixkcMzQaGy8xhwsKHiEfBQkSIg+G
BAcUCIIBBDSYYGiAAUMALFR6FAgAOw==
""".translate(__cleanup)

IMG_ERR_B64 = """
R0lGODlhEAAQAKEBAAAAAP///////////yH5BAEKAAIALAAAAAAQABAAAAIwlBWpeR0AIwwNPRmZuVNJinyWuClhBlZjpm5fqnIAHJPtOd3Hou9mL6NVgj2L
plEAADs=
""".translate(__cleanup)


# Main window clicks
def system_url(system_name: str) -> str:
    if this.system_address:
        return requests.utils.requote_uri(f'https://www.edsm.net/en/system?systemID64={this.system_address}')

    if system_name:
        return requests.utils.requote_uri(f'https://www.edsm.net/en/system?systemName={system_name}')

    return ''


def station_url(system_name: str, station_name: str) -> str:
    if system_name and station_name:
        return requests.utils.requote_uri(f'https://www.edsm.net/en/system?systemName={system_name}&stationName={station_name}')

    # monitor state might think these are gone, but we don't yet
    if this.system and this.station:
        return requests.utils.requote_uri(f'https://www.edsm.net/en/system?systemName={this.system}&stationName={this.station}')

    if system_name:
        return requests.utils.requote_uri(f'https://www.edsm.net/en/system?systemName={system_name}&stationName=ALL')

    return ''

def plugin_start3(plugin_dir: str) -> str:
    # Can't be earlier since can only call PhotoImage after window is created
    this._IMG_KNOWN = tk.PhotoImage(data=IMG_KNOWN_B64)  # green circle
    this._IMG_UNKNOWN = tk.PhotoImage(data=IMG_UNKNOWN_B64)  # red circle
    this._IMG_NEW = tk.PhotoImage(data=IMG_NEW_B64)
    this._IMG_ERROR = tk.PhotoImage(data=IMG_ERR_B64)  # BBC Mode 5 '?'

    # Migrate old settings
    if not config.get('edsm_cmdrs'):
        if isinstance(config.get('cmdrs'), list) and config.get('edsm_usernames') and config.get('edsm_apikeys'):
            # Migrate <= 2.34 settings
            config.set('edsm_cmdrs', config.get('cmdrs'))

        elif config.get('edsm_cmdrname'):
            # Migrate <= 2.25 settings. edsm_cmdrs is unknown at this time
            config.set('edsm_usernames', [config.get('edsm_cmdrname') or ''])
            config.set('edsm_apikeys',   [config.get('edsm_apikey') or ''])

        config.delete('edsm_cmdrname')
        config.delete('edsm_apikey')

    if config.getint('output') & 256:
        # Migrate <= 2.34 setting
        config.set('edsm_out', 1)

    config.delete('edsm_autoopen')
    config.delete('edsm_historical')

    this.thread = Thread(target=worker, name='EDSM worker')
    this.thread.daemon = True
    this.thread.start()

    return 'EDSM'


def plugin_app(parent: tk.Tk) -> None:
    this.system_link = parent.children['system']  # system label in main window
    this.system_link.bind_all('<<EDSMStatus>>', update_status)
    this.station_link = parent.children['station']  # station label in main window


def plugin_stop() -> None:
    # Signal thread to close and wait for it
    this.queue.put(None)
    this.thread.join()
    this.thread = None
    # Suppress 'Exception ignored in: <function Image.__del__ at ...>' errors
    this._IMG_KNOWN = this._IMG_UNKNOWN = this._IMG_NEW = this._IMG_ERROR = None


def plugin_prefs(parent: tk.Tk, cmdr: str, is_beta: bool) -> tk.Frame:
    PADX = 10  # noqa: N806
    BUTTONX = 12  # indent Checkbuttons and Radiobuttons # noqa: N806
    PADY = 2		# close spacing # noqa: N806

    frame = nb.Frame(parent)
    frame.columnconfigure(1, weight=1)

    HyperlinkLabel(
        frame,
        text='Elite Dangerous Star Map',
        background=nb.Label().cget('background'),
        url='https://www.edsm.net/',
        underline=True
    ).grid(columnspan=2, padx=PADX, sticky=tk.W)  # Don't translate

    this.log = tk.IntVar(value=config.getint('edsm_out') and 1)
    this.log_button = nb.Checkbutton(
        frame, text=_('Send flight log and Cmdr status to EDSM'), variable=this.log, command=prefsvarchanged
    )

    this.log_button.grid(columnspan=2, padx=BUTTONX, pady=(5, 0), sticky=tk.W)

    nb.Label(frame).grid(sticky=tk.W)  # big spacer
    # Section heading in settings
    this.label = HyperlinkLabel(
        frame,
        text=_('Elite Dangerous Star Map credentials'),
        background=nb.Label().cget('background'),
        url='https://www.edsm.net/settings/api',
        underline=True
    )

    this.label.grid(columnspan=2, padx=PADX, sticky=tk.W)

    this.cmdr_label = nb.Label(frame, text=_('Cmdr'))  # Main window
    this.cmdr_label.grid(row=10, padx=PADX, sticky=tk.W)
    this.cmdr_text = nb.Label(frame)
    this.cmdr_text.grid(row=10, column=1, padx=PADX, pady=PADY, sticky=tk.W)

    this.user_label = nb.Label(frame, text=_('Commander Name'))  # EDSM setting
    this.user_label.grid(row=11, padx=PADX, sticky=tk.W)
    this.user = nb.Entry(frame)
    this.user.grid(row=11, column=1, padx=PADX, pady=PADY, sticky=tk.EW)

    this.apikey_label = nb.Label(frame, text=_('API Key'))  # EDSM setting
    this.apikey_label.grid(row=12, padx=PADX, sticky=tk.W)
    this.apikey = nb.Entry(frame)
    this.apikey.grid(row=12, column=1, padx=PADX, pady=PADY, sticky=tk.EW)

    prefs_cmdr_changed(cmdr, is_beta)

    return frame


def prefs_cmdr_changed(cmdr: str, is_beta: bool) -> None:
    this.log_button['state'] = cmdr and not is_beta and tk.NORMAL or tk.DISABLED
    this.user['state'] = tk.NORMAL
    this.user.delete(0, tk.END)
    this.apikey['state'] = tk.NORMAL
    this.apikey.delete(0, tk.END)
    if cmdr:
        this.cmdr_text['text'] = f'{cmdr}{" [Beta]" if is_beta else ""}'
        cred = credentials(cmdr)

        if cred:
            this.user.insert(0, cred[0])
            this.apikey.insert(0, cred[1])

    else:
        this.cmdr_text['text'] = _('None') 	# No hotkey/shortcut currently defined

    to_set = tk.DISABLED
    if cmdr and not is_beta and this.log.get():
        to_set = tk.NORMAL

    set_prefs_ui_states(to_set)


def prefsvarchanged() -> None:
    to_set = tk.DISABLED
    if this.log.get():
        to_set = this.log_button['state']

    set_prefs_ui_states(to_set)


def set_prefs_ui_states(state: str) -> None:
    """
    Set the state of various config UI entries

    :param state: the state to set each entry to
    """
    this.label['state'] = state
    this.cmdr_label['state'] = state
    this.cmdr_text['state'] = state
    this.user_label['state'] = state
    this.user['state'] = state
    this.apikey_label['state'] = state
    this.apikey['state'] = state


def prefs_changed(cmdr: str, is_beta: bool) -> None:
    config.set('edsm_out', this.log.get())

    if cmdr and not is_beta:
        # TODO: remove this when config is rewritten.
        cmdrs: List[str] = list(config.get('edsm_cmdrs') or [])
        usernames: List[str] = list(config.get('edsm_usernames') or [])
        apikeys: List[str] = list(config.get('edsm_apikeys') or [])
        if cmdr in cmdrs:
            idx = cmdrs.index(cmdr)
            usernames.extend([''] * (1 + idx - len(usernames)))
            usernames[idx] = this.user.get().strip()
            apikeys.extend([''] * (1 + idx - len(apikeys)))
            apikeys[idx] = this.apikey.get().strip()

        else:
            config.set('edsm_cmdrs', cmdrs + [cmdr])
            usernames.append(this.user.get().strip())
            apikeys.append(this.apikey.get().strip())

        config.set('edsm_usernames', usernames)
        config.set('edsm_apikeys', apikeys)


def credentials(cmdr: str) -> Optional[Tuple[str, str]]:
    # Credentials for cmdr
    if not cmdr:
        return None

    cmdrs = config.get('edsm_cmdrs')
    if not cmdrs:
        # Migrate from <= 2.25
        cmdrs = [cmdr]
        config.set('edsm_cmdrs', cmdrs)

    if cmdr in cmdrs and config.get('edsm_usernames') and config.get('edsm_apikeys'):
        idx = cmdrs.index(cmdr)
        return (config.get('edsm_usernames')[idx], config.get('edsm_apikeys')[idx])

    else:
        return None


def journal_entry(
    cmdr: str, is_beta: bool, system: str, station: str, entry: MutableMapping[str, Any], state: Mapping[str, Any]
) -> None:
    if entry['event'] in ('CarrierJump', 'FSDJump', 'Location', 'Docked'):
        logger.debug(f'''{entry["event"]}
Commander: {cmdr}
System: {system}
Station: {station}
state: {state!r}
entry: {entry!r}'''
                    )
    # Always update our system address even if we're not currently the provider for system or station, but dont update
    # on events that contain "future" data, such as FSDTarget
    if entry['event'] in ('Location', 'Docked', 'CarrierJump', 'FSDJump'):
        this.system_address = entry.get('SystemAddress') or this.system_address
        this.system = entry.get('StarSystem') or this.system

    # We need pop == 0 to set the value so as to clear 'x' in systems with
    # no stations.
    pop = entry.get('Population')
    if pop is not None:
        this.system_population = pop

    this.station = entry.get('StationName') or this.station
    this.station_marketid = entry.get('MarketID') or this.station_marketid
    # We might pick up StationName in DockingRequested, make sure we clear it if leaving
    if entry['event'] in ('Undocked', 'FSDJump', 'SupercruiseEntry'):
        this.station = None
        this.station_marketid = None

    if config.get('station_provider') == 'EDSM':
        to_set = this.station
        if not this.station:
            if this.system_population and this.system_population > 0:
                to_set = STATION_UNDOCKED

            else:
                to_set = ''

        this.station_link['text'] = to_set
        this.station_link['url'] = station_url(this.system, str(this.station))
        this.station_link.update_idletasks()

    # Update display of 'EDSM Status' image
    if this.system_link['text'] != system:
        this.system_link['text'] = system or ''
        this.system_link['image'] = ''
        this.system_link.update_idletasks()

    this.multicrew = bool(state['Role'])
    if 'StarPos' in entry:
        this.coordinates = entry['StarPos']

    elif entry['event'] == 'LoadGame':
        this.coordinates = None

    if entry['event'] in ['LoadGame', 'Commander', 'NewCommander']:
        this.newgame = True
        this.newgame_docked = False
        this.navbeaconscan = 0

    elif entry['event'] == 'StartUp':
        this.newgame = False
        this.newgame_docked = False
        this.navbeaconscan = 0

    elif entry['event'] == 'Location':
        this.newgame = True
        this.newgame_docked = entry.get('Docked', False)
        this.navbeaconscan = 0

    elif entry['event'] == 'NavBeaconScan':
        this.navbeaconscan = entry['NumBodies']

    # Send interesting events to EDSM
    if (
        config.getint('edsm_out') and not is_beta and not this.multicrew and credentials(cmdr) and
        entry['event'] not in this.discardedEvents
    ):
        # Introduce transient states into the event
        transient = {
            '_systemName': system,
            '_systemCoordinates': this.coordinates,
            '_stationName': station,
            '_shipId': state['ShipID'],
        }

        entry.update(transient)

        if entry['event'] == 'LoadGame':
            # Synthesise Materials events on LoadGame since we will have missed it
            materials = {
                'timestamp': entry['timestamp'],
                'event': 'Materials',
                'Raw':          [{'Name': k, 'Count': v} for k, v in state['Raw'].items()],
                'Manufactured': [{'Name': k, 'Count': v} for k, v in state['Manufactured'].items()],
                'Encoded':      [{'Name': k, 'Count': v} for k, v in state['Encoded'].items()],
            }
            materials.update(transient)
            this.queue.put((cmdr, materials))

        if entry['event'] in ('CarrierJump', 'FSDJump', 'Location', 'Docked'):
            logger.debug(f'''{entry["event"]}
Queueing: {entry!r}'''
                         )
        this.queue.put((cmdr, entry))


# Update system data
def cmdr_data(data: Mapping[str, Any], is_beta: bool) -> None:
    system = data['lastSystem']['name']

    # Always store initially, even if we're not the *current* system provider.
    if not this.station_marketid:
        this.station_marketid = data['commander']['docked'] and data['lastStarport']['id']

    # Only trust CAPI if these aren't yet set
    this.system = this.system or data['lastSystem']['name']
    this.station = this.station or data['commander']['docked'] and data['lastStarport']['name']
    # TODO: Fire off the EDSM API call to trigger the callback for the icons

    if config.get('system_provider') == 'EDSM':
        this.system_link['text'] = this.system
        # Do *NOT* set 'url' here, as it's set to a function that will call
        # through correctly.  We don't want a static string.
        this.system_link.update_idletasks()

    if config.get('station_provider') == 'EDSM':
        if data['commander']['docked']:
            this.station_link['text'] = this.station

        elif data['lastStarport']['name'] and data['lastStarport']['name'] != "":
            this.station_link['text'] = STATION_UNDOCKED

        else:
            this.station_link['text'] = ''

        # Do *NOT* set 'url' here, as it's set to a function that will call
        # through correctly.  We don't want a static string.

        this.station_link.update_idletasks()

    if not this.system_link['text']:
        this.system_link['text'] = system
        this.system_link['image'] = ''
        this.system_link.update_idletasks()


# Worker thread
def worker() -> None:
    pending = []  # Unsent events
    closing = False

    while True:
        item: Optional[Tuple[str, Mapping[str, Any]]] = this.queue.get()
        if item:
            (cmdr, entry) = item
        else:
            closing = True  # Try to send any unsent events before we close

        retrying = 0
        while retrying < 3:
            try:
                # TODO: Technically entry can be unbound here.
                if item and entry['event'] in ('CarrierJump', 'FSDJump', 'Location', 'Docked'):
                    logger.debug(f'{entry["event"]}')

                if item and entry['event'] not in this.discardedEvents:
                    if entry['event'] in ('CarrierJump', 'FSDJump', 'Location', 'Docked'):
                        logger.debug(f'{entry["event"]} event not in discarded list')
                    pending.append(entry)

                # Get list of events to discard
                if not this.discardedEvents:
                    r = this.session.get('https://www.edsm.net/api-journal-v1/discard', timeout=_TIMEOUT)
                    r.raise_for_status()
                    this.discardedEvents = set(r.json())
                    this.discardedEvents.discard('Docked')  # should_send() assumes that we send 'Docked' events
                    assert this.discardedEvents			# wouldn't expect this to be empty
                    # Filter out unwanted events
                    pending = [x for x in pending if x['event'] not in this.discardedEvents]

                if should_send(pending):
                    if any([p for p in pending if p['event'] in ('CarrierJump', 'FSDJump', 'Location', 'Docked')]):
                        logger.debug('CarrierJump (or FSDJump) in pending and it passed should_send()')

                    (username, apikey) = credentials(cmdr)  # TODO: This raises if credentials returns None
                    data = {
                        'commanderName': username.encode('utf-8'),
                        'apiKey': apikey,
                        'fromSoftware': applongname,
                        'fromSoftwareVersion': appversion,
                        'message': json.dumps(pending, ensure_ascii=False).encode('utf-8'),
                    }

                    if any([p for p in pending if p['event'] in ('CarrierJump', 'FSDJump', 'Location', 'Docked')]):
                        data_elided = data.copy()
                        data_elided['apiKey'] = '<elided>'
                        logger.debug(f'CarrierJump (or FSDJump): Attempting API call\ndata: {data_elided!r}')
                    r = this.session.post('https://www.edsm.net/api-journal-v1', data=data, timeout=_TIMEOUT)
                    r.raise_for_status()
                    reply = r.json()
                    (msg_num, msg) = reply['msgnum'], reply['msg']
                    # 1xx = OK
                    # 2xx = fatal error
                    # 3&4xx not generated at top-level
                    # 5xx = error but events saved for later processing
                    if msg_num // 100 == 2:
                        logger.warning(f'EDSM\t{msg_num} {msg}\t{json.dumps(pending, separators=(",", ": "))}')
                        plug.show_error(_('Error: EDSM {MSG}').format(MSG=msg))

                    else:
                        for e, r in zip(pending, reply['events']):
                            if not closing and e['event'] in ['StartUp', 'Location', 'FSDJump', 'CarrierJump']:
                                # Update main window's system status
                                this.lastlookup = r
                                # calls update_status in main thread
                                this.system_link.event_generate('<<EDSMStatus>>', when="tail")

                            elif r['msgnum'] // 100 != 1:
                                logger.warning(f'EDSM\t{r["msgnum"]} {r["msg"]}\t'
                                               f'{json.dumps(e, separators = (",", ": "))}')

                        pending = []

                break
            except Exception as e:
                logger.debug('Sending API events', exc_info=e)
                retrying += 1

        else:
            plug.show_error(_("Error: Can't connect to EDSM"))

        if closing:
            return


# Whether any of the entries should be sent immediately
def should_send(entries: List[Mapping[str, Any]]) -> bool:
    # batch up burst of Scan events after NavBeaconScan
    if this.navbeaconscan:
        if entries and entries[-1]['event'] == 'Scan':
            this.navbeaconscan -= 1
            if this.navbeaconscan:
                return False

        else:
            assert(False)
            this.navbeaconscan = 0

    for entry in entries:
        if (entry['event'] == 'Cargo' and not this.newgame_docked) or entry['event'] == 'Docked':
            # Cargo is the last event on startup, unless starting when docked in which case Docked is the last event
            this.newgame = False
            this.newgame_docked = False
            return True

        elif this.newgame:
            pass

        elif entry['event'] not in [
                'CommunityGoal',  # Spammed periodically
                'ModuleBuy', 'ModuleSell', 'ModuleSwap',		# will be shortly followed by "Loadout"
                'ShipyardBuy', 'ShipyardNew', 'ShipyardSwap']:  # "
            return True

    return False


# Call edsm_notify_system() in this and other interested plugins with EDSM's response to a 'StartUp', 'Location',
# 'FSDJump' or 'CarrierJump' event
def update_status(event=None) -> None:
    for plugin in plug.provides('edsm_notify_system'):
        plug.invoke(plugin, None, 'edsm_notify_system', this.lastlookup)


# Called with EDSM's response to a 'StartUp', 'Location', 'FSDJump' or 'CarrierJump' event.
# https://www.edsm.net/en/api-journal-v1
# msgnum: 1xx = OK, 2xx = fatal error, 3xx = error, 4xx = ignorable errors.
def edsm_notify_system(reply: Mapping[str, Any]) -> None:
    if not reply:
        this.system_link['image'] = this._IMG_ERROR
        plug.show_error(_("Error: Can't connect to EDSM"))

    elif reply['msgnum'] // 100 not in (1, 4):
        this.system_link['image'] = this._IMG_ERROR
        plug.show_error(_('Error: EDSM {MSG}').format(MSG=reply['msg']))

    elif reply.get('systemCreated'):
        this.system_link['image'] = this._IMG_NEW

    else:
        this.system_link['image'] = this._IMG_KNOWN
