#!/usr/bin/env python3
"""check_zte_h388x - plugin Nagios/Icinga per modem ZTE H388X.

Sequenza di login (come il browser):
  home -> login_entry -> login_token -> POST login_entry
  -> (se login_need_refresh) reload home -> conferma firewall_homepage
Poi per ogni dato: open_page della pagina del menu -> menuData.

Credenziali da file KEY=VALUE con ZTE_USER e ZTE_PASS.
"""

import argparse
import hashlib
import http.cookiejar
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import re  # aggiungere in cima al file, con gli altri import

MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


OK, WARNING, CRITICAL, UNKNOWN = 0, 1, 2, 3
LABEL = {OK: "OK", WARNING: "WARNING", CRITICAL: "CRITICAL", UNKNOWN: "UNKNOWN"}
STATE_DIR = "/var/tmp/check_zte_h388x"

# Lo User-Agent deve coincidere con quello del browser: il modem lega la sessione a questo valore.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
LOGIN = "/?_type=loginData&_tag="
JSON_ACCEPT = "application/json, text/javascript, */*; q=0.01"
XML_ACCEPT = "application/xml, text/xml, */*; q=0.01"
TIMEOUT = 10
DEBUG = False


class PluginError(Exception):
    pass


def dbg(msg):
    if DEBUG:
        print(f"[debug] {msg}", file=sys.stderr)


def load_credentials(path):
    if os.name == "posix" and os.stat(path).st_mode & 0o077:
        raise PluginError(f"{path} accessibile da altri utenti (usa chmod 600)")
    creds = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                creds[key.strip()] = value.strip()
    try:
        return creds["ZTE_USER"], creds["ZTE_PASS"]
    except KeyError as e:
        raise PluginError(f"chiave {e} mancante in {path}")


def instances(elem):
    """Ogni <Instance> diventa un dict; le chiavi perdono il prefisso (OBJ_X.IPAddr -> IPAddr)."""
    for inst in elem.iter("Instance"):
        names = [c.text for c in inst if c.tag == "ParaName"]
        values = [c.text for c in inst if c.tag == "ParaValue"]
        yield {n.rsplit(".", 1)[-1]: v for n, v in zip(names, values)}


def section(root, tag):
    elem = root.find(tag)
    if elem is None:
        raise PluginError(f"sezione {tag} assente nella risposta")
    return elem


class Modem:
    def __init__(self, host):
        self.host = host
        self.base = f"http://{host}"
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        # cookie che il browser invia sempre
        self.jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name="_TESTCOOKIESUPPORT",
                value="1",
                port=None,
                port_specified=False,
                domain=host,
                domain_specified=False,
                domain_initial_dot=False,
                path="/",
                path_specified=True,
                secure=False,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
        )

    def _request(self, path, data=None, ajax=True, accept=None, referer=True):
        headers = {"User-Agent": UA, "Cache-Control": "no-cache", "Pragma": "no-cache"}
        if referer:
            headers["Referer"] = self.base + "/"
        if accept:
            headers["Accept"] = accept
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers["Origin"] = self.base
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        req = urllib.request.Request(self.base + path, data=body, headers=headers)
        with self.opener.open(req, timeout=TIMEOUT) as r:
            text = r.read().decode("utf-8", errors="replace")
        dbg(f"{'POST' if body else 'GET'} {path[:60]} -> {len(text)} byte")
        return text

    def _cookie(self, name):
        value = None
        for c in self.jar:
            if c.name == name:
                value = c.value
        if value is None:
            raise PluginError(f"cookie {name} assente")
        return value

    def login(self, user, password):
        self._request("/", ajax=False, referer=False)  # 1. home
        meta = json.loads(self._request(LOGIN + "login_entry", accept=JSON_ACCEPT))
        if meta.get("loginErrMsg"):
            raise PluginError("login non disponibile: " + meta["loginErrMsg"].strip())
        challenge = "".join(
            ET.fromstring(
                self._request(LOGIN + "login_token", accept=XML_ACCEPT)
            ).itertext()
        )  # 2. challenge
        digest = hashlib.sha256((password + challenge).encode("utf-8")).hexdigest()
        resp = json.loads(
            self._request(
                LOGIN + "login_entry",
                accept=JSON_ACCEPT,
                data={  # 3. POST
                    "Password": digest,
                    "Username": user,
                    "_sessionTOKEN": meta["sess_token"],
                    "action": "login",
                },
            )
        )
        if resp.get("loginErrMsg"):
            raise PluginError("login fallito: " + resp["loginErrMsg"].strip())
        if int(resp.get("lockingTime") or 0) != 0:
            raise PluginError(f"account bloccato (lockingTime={resp['lockingTime']})")
        if resp.get("login_need_refresh"):
            # come il browser: il modem chiede il reload della home, ed è da lì che il SID diventa valido
            self._request("/", ajax=False)
            dbg(f"SID dopo il reload: {self._cookie('SID')[:8]}...")
        # 4. conferma: stessa richiesta che fa il browser dopo il login
        check = self._request(
            f"/?_type=menuData&_tag=firewall_homepage_lua.lua"
            f"&_={int(time.time() * 1000)}",
            accept=XML_ACCEPT,
        )
        if "SessionTimeout" in check:
            raise PluginError("sessione non valida dopo il login")
        if "SUCC" not in check:
            raise PluginError("conferma di login senza esito SUCC")

    def open_page(self, page):
        """Apre una pagina del menu, come fa il browser prima di chiedere i suoi dati."""
        self._request(
            f"/?_type=menuView&_tag={page}&Menu3Location=0&_={int(time.time() * 1000)}",
            accept="*/*",
        )

    def get_xml(self, tag):
        path = f"/?_type=menuData&_tag={tag}&_={int(time.time() * 1000)}"
        text = self._request(path, accept=XML_ACCEPT)
        if "SessionTimeout" in text:
            raise PluginError("sessione non valida (SessionTimeout)")
        root = ET.fromstring(text)
        err = root.findtext("IF_ERRORSTR", "SUCC")
        if err != "SUCC":
            raise PluginError(f"errore modem: {err}")
        return root


def load_state(name):
    try:
        with open(os.path.join(STATE_DIR, name + ".json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(name, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, name + ".json"), "w") as f:
        json.dump(state, f)


def rate_kbps(cur, prev, dt):
    """kbit/s fra due campionamenti; None se il contatore è sceso (riavvio del modem)."""
    if cur < prev:
        return None
    return (cur - prev) * 8 / dt / 1000


def check_traffic(modem, args):
    modem.open_page("localNetStatus")
    root = modem.get_xml("eth_lanstatus_lua.lua")
    now = time.time()
    ports = {}
    for i in instances(section(root, "OBJ_ETH_ID")):
        name = i.get("AliasName", i["_InstID"])  # LAN1..LAN4
        ports[name] = {
            "rx": int(i["BytesReceived"]),
            "tx": int(i["BytesSent"]),
            "status": i["Status"],
        }

    prev = load_state("traffic")
    status, parts, perf = OK, [], []

    down = [n for n in args.ports if ports.get(n, {}).get("status") != "Up"]
    if down:
        status = CRITICAL
        parts.append("porte down: " + ", ".join(down))

    if prev.get("ts") and now > prev["ts"]:
        dt = now - prev["ts"]
        for name, p in ports.items():
            old = prev.get("ports", {}).get(name)
            if not old or p["status"] != "Up":
                continue
            rx = rate_kbps(p["rx"], old["rx"], dt)
            tx = rate_kbps(p["tx"], old["tx"], dt)
            if rx is None or tx is None:
                parts.append(f"{name} contatori azzerati")
                continue
            parts.append(f"{name} RX {rx:.0f} TX {tx:.0f} kbit/s")
            perf.append(f"'{name}_rx_kbps'={rx:.2f}kbps '{name}_tx_kbps'={tx:.2f}kbps")
            if args.warn is not None and max(rx, tx) > args.warn:
                status = max(status, WARNING)
    else:
        parts.append("primo campionamento")

    # contatori grezzi: utili a un backend (Graphite, InfluxDB) che calcola la derivata
    for name, p in ports.items():
        perf.append(f"'{name}_rx_bytes'={p['rx']}c '{name}_tx_bytes'={p['tx']}c")

    save_state("traffic", {"ts": now, "ports": ports})
    return status, "; ".join(parts), " ".join(perf)

def wifi_label(ssid, band):
    """Etichetta stabile per performanceLabel: categoria di rete + banda."""
    name = ssid.lower()
    if "mesh" in name:
        kind = "Mesh"
    elif "guest" in name:
        kind = "Guest"
    else:
        kind = "Privata"
    b = "2G" if "2.4" in band else "5G" if "5" in band else "XG"
    return f"{kind}_{b}"



def check_wifi(modem, args):
    modem.open_page("localNetStatus")
    root = modem.get_xml("wlan_status_lua.lua")
    bands = {
        i["_InstID"]: i.get("Band", "?")
        for i in instances(section(root, "OBJ_WLANSETTING_ID"))
    }
    aps = {}
    for i in instances(section(root, "OBJ_WLANAP_ID")):
        aps[i["_InstID"]] = {
            "ssid": i.get("ESSID", "?"),
            "enabled": i.get("Enable") == "1",
            "band": bands.get(i.get("WLANViewName"), "?"),
        }
    counters = {}
    for i in instances(section(root, "OBJ_WLANCONFIGDRV_ID")):
        counters[i["_InstID"]] = [
            int(i["TotalBytesReceived"]),
            int(i["TotalBytesSent"]),
        ]

    active = {k: v for k, v in aps.items() if v["enabled"]}
    now = time.time()
    prev = load_state("wifi")
    status = OK if active else WARNING
    parts = [f"{len(active)} SSID attivi"]
    perf = []

    if prev.get("ts") and now > prev["ts"]:
        dt = now - prev["ts"]
        for k, v in active.items():
            old = prev.get("counters", {}).get(k)
            cur = counters.get(k, [0, 0])
            if not old:
                continue
            rx = rate_kbps(cur[0], old[0], dt)
            tx = rate_kbps(cur[1], old[1], dt)
            if rx is None or tx is None:
                continue
            parts.append(f"{v['ssid']} ({v['band']}) RX {rx:.0f} TX {tx:.0f} kbit/s")

    for k, v in active.items():
        lab = wifi_label(v["ssid"], v["band"])
        cur = counters.get(k, [0, 0])
        perf.append(f"'{lab}_rx_bytes'={cur[0]}c '{lab}_tx_bytes'={cur[1]}c")

    save_state(
        "wifi", {"ts": now, "counters": {k: counters.get(k, [0, 0]) for k in active}}
    )
    return status, "; ".join(parts), " ".join(perf)


def check_wan(modem, args):
    modem.open_page("ethWanStatus")  # pagina WAN Ethernet/SFP, come nel browser
    root = modem.get_xml("eth_interface_status_lua.lua")
    ports = list(instances(section(root, "OBJ_ETH_ID")))
    if not ports:
        raise PluginError("nessuna interfaccia WAN nella risposta")
    p = ports[0]

    status = OK
    reasons = []
    link = f"{p.get('LinkSpeed', '?')}M {p.get('LinkDuplex', '?')}"
    if p.get("Status") != "Up":
        status = CRITICAL
        reasons.append(f"link {p.get('Status', '?')}")
    elif args.expect_speed and p.get("LinkSpeed") != str(args.expect_speed):
        status = WARNING
        reasons.append(f"velocità {p.get('LinkSpeed')} invece di {args.expect_speed}")

    rx_dbm = float(p.get("singleRXStrength", "nan"))
    tx_dbm = float(p.get("singleTXStrength", "nan"))
    if args.warn is not None and rx_dbm < args.warn:
        status = max(status, WARNING)
        reasons.append(f"RX {rx_dbm} dBm sotto {args.warn}")
    if args.crit is not None and rx_dbm < args.crit:
        status = CRITICAL
        reasons.append(f"RX {rx_dbm} dBm sotto {args.crit}")

    msg = f"WAN {p.get('_InstID', '?').split('.')[-1]} {p.get('Status', '?')} {link}, RX {rx_dbm} dBm, TX {tx_dbm} dBm"
    if reasons:
        msg += " - " + "; ".join(reasons)
    perf = (
        f"'wan_rx_dbm'={rx_dbm} 'wan_tx_dbm'={tx_dbm} "
        f"'wan_rx_bytes'={p.get('BytesReceived', 0)}c "
        f"'wan_tx_bytes'={p.get('BytesSent', 0)}c"
    )
    return status, msg, perf

def load_dhcp_names(modem):
    """Nomi assegnati a mano nei binding DHCP, indicizzati per MAC."""
    modem.open_page("lanMgrIpv4")
    root = modem.get_xml("dhcp4s_dhcpbinding_m.lua")
    names = {}
    for i in instances(section(root, "OBJ_DHCPBIND_ID")):
        mac = i.get("MACAddr", "").upper()
        if mac and i.get("Name"):
            names[mac] = i["Name"]
    return names


def read_allowed(path):
    """Legge MAC autorizzati e hostname.

    Formato di ogni riga (il commento dopo '#' viene ignorato):
        mac hostname   # commento
        mac,hostname   # commento
        mac            # senza hostname
    Restituisce {MAC: hostname}, hostname può essere ''.
    """
    allowed = {}
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            data = line.split("#", 1)[0].strip()
            if not data:
                continue
            parts = [p for p in data.replace(",", " ").split() if p]
            if len(parts) > 2:
                raise PluginError(
                    f"{path}:{lineno}: troppi campi, usa 'mac hostname' "
                    "e '#' per il commento")
            mac = parts[0].upper()
            if not MAC_RE.match(mac):
                raise PluginError(f"{path}:{lineno}: MAC non valido '{parts[0]}'")
            allowed[mac] = parts[1] if len(parts) == 2 else ""
    return allowed

def ip_key_of(h):
    try:
        return tuple(int(p) for p in h["ip"].split("."))
    except ValueError:
        return (999, 999, 999, 999)

def render_table(hosts, is_known):
    """Tabella senza bordi: colonne allineate con spazi."""
    headers = ["HOSTNAME", "IP", "MAC", "PORTA/SSID", "TIPO", "STATO"]
    rows = [[h["name"] or "(nessun nome)", h["ip"], h["mac"], h["via"],
             h["kind"], "noto" if is_known(h) else "SCONOSCIUTO"]
            for h in sorted(hosts, key=ip_key_of)]

    widths = [len(c) for c in headers]
    for r in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, r)]

    def line(cells):
        return "  ".join(f"{c:<{w}}" for c, w in zip(cells, widths)).rstrip()

    out = [line(headers), "  ".join(" " * w for w in widths).rstrip()]
    out += [line(r) for r in rows]
    return out


def render_table_border(hosts, is_known):
    """Tabella con bordi +, - e |; le colonne si adattano al contenuto."""
    headers = ["HOSTNAME", "IP", "MAC", "PORTA/SSID", "TIPO", "STATO"]
    rows = [[h["name"] or "(nessun nome)", h["ip"], h["mac"], h["via"],
             h["kind"], "noto" if is_known(h) else "SCONOSCIUTO"]
            for h in sorted(hosts, key=ip_key_of)]

    widths = [len(c) for c in headers]
    for r in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, r)]

    def border():
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def line(cells):
        return "|" + "|".join(f" {c:<{w}} " for c, w in zip(cells, widths)) + "|"

    out = [border(), line(headers), border()]
    out += [line(r) for r in rows]
    out.append(border())
    return out


def check_hosts(modem, args):
    modem.open_page("localNetStatus")
    hosts, errors = [], []
    for tag, kind in (("accessdev_ssiddev_lua.lua", "WiFi"),
                      ("accessdev_landevs_lua.lua", "LAN")):
        try:
            root = modem.get_xml(tag)
        except PluginError as e:
            errors.append(f"{kind}: {e}")
            continue
        for i in instances(section(root, "OBJ_ACCESSDEV_ID")):
            hosts.append({
                "device_name": i.get("HostName", ""),
                "ip": i.get("IPAddress", "?"),
                "mac": i.get("MACAddress", "").upper(),
                "kind": kind,
                "via": i.get("AliasName", "?"),   # SSID per WiFi, porta per LAN
            })
    if errors:
        raise PluginError("dati host incompleti: " + "; ".join(errors))

    # nome risolto: prima il commento nel file dei MAC, poi il nome annunciato dal dispositivo
    allowed = read_allowed(args.allowed_macs) if args.allowed_macs else None
    for h in hosts:
        h["name"] = (allowed or {}).get(h["mac"]) or h["device_name"] or ""

    def is_known(h):
        return allowed is None or h["mac"] in allowed

    count = len(hosts)
    wifi_n = sum(1 for h in hosts if h["kind"] == "WiFi")
    unknown = [h for h in hosts if not is_known(h)]

    # conteggio per interfaccia: porte LAN e SSID
    per_iface = {}
    for h in hosts:
        s = per_iface.setdefault(h["via"], {"known": 0, "unknown": 0})
        s["known" if is_known(h) else "unknown"] += 1

    warn = int(args.warn) if args.warn is not None else None
    crit = int(args.crit) if args.crit is not None else None
    status = OK
    reasons = []
    if crit is not None and count >= crit:
        status = CRITICAL
        reasons.append(f"soglia critical {crit} raggiunta")
    elif warn is not None and count >= warn:
        status = WARNING
        reasons.append(f"soglia warning {warn} raggiunta")
    if unknown:
        status = max(status, WARNING)

    # messaggio di stato, senza '|' (lo usa Icinga come separatore delle perfdata)
    msg = f"{count} host connessi ({wifi_n} WiFi, {count - wifi_n} LAN)"
    if reasons:
        msg += " - " + "; ".join(reasons)
    details = [f"{iface}: {per_iface[iface]['known']} noti, "
               f"{per_iface[iface]['unknown']} sconosciuti"
               for iface in sorted(per_iface)]
    msg += " [" + " ; ".join(details) + "]"
    if unknown:
        listed = [f"{h['name'] or '(nessun nome)'}({h['ip']}, {h['mac']}, {h['via']})"
                  for h in unknown]
        msg += " - sconosciuti: " + ", ".join(listed)

    # perfdata: totali, per tipo e per interfaccia
    perf_parts = [
        f"'hosts'={count};{warn or ''};{crit or ''}",
        f"'hosts_wifi'={wifi_n}",
        f"'hosts_lan'={count - wifi_n}",
        f"'hosts_unknown'={len(unknown)}",
    ]
    for iface in sorted(per_iface):
        label = iface.replace(" ", "_")
        perf_parts.append(f"'{label}_known'={per_iface[iface]['known']}")
        perf_parts.append(f"'{label}_unknown'={per_iface[iface]['unknown']}")
    perf = " ".join(perf_parts)

    table = render_table(hosts, is_known)
    return status, msg, perf, table


def main():
    global TIMEOUT, DEBUG
    p = argparse.ArgumentParser(description="Check Nagios/Icinga per ZTE H388X")
    p.add_argument("-H", "--host", required=True, help="IP del modem")
    p.add_argument(
        "-m", "--mode", choices=["traffic", "wifi", "wan", "hosts"], required=True
    )
    p.add_argument(
        "--expect-speed",
        type=int,
        default=0,
        help="velocità attesa in Mbps per la WAN (es. 1000)",
    )
    p.add_argument(
        "--creds", default="/etc/nagios/zte.env", help="file con ZTE_USER e ZTE_PASS"
    )
    p.add_argument(
        "-p",
        "--ports",
        default="",
        help="porte LAN che devono essere Up, es. LAN2,LAN4 (solo traffic)",
    )
    p.add_argument("-w", "--warn", type=float, help="soglia warning")
    p.add_argument("-c", "--crit", type=float, help="soglia critical (solo hosts)")
    p.add_argument("--allowed-macs", help="file con MAC autorizzati, uno per riga")
    p.add_argument("-t", "--timeout", type=int, default=10)
    p.add_argument("-d", "--debug", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="stampa l'elenco host ")
    args = p.parse_args()

    TIMEOUT = args.timeout
    DEBUG = args.debug
    args.ports = [x.strip() for x in args.ports.split(",") if x.strip()]

    try:
        user, password = load_credentials(args.creds)
        modem = Modem(args.host)
        modem.login(user, password)
        if args.mode == "traffic":
            status, msg, perf = check_traffic(modem, args)
        elif args.mode == "wifi":
            status, msg, perf = check_wifi(modem, args)
        elif args.mode == "hosts":
            status, msg, perf, table  = check_hosts(modem, args)
        elif args.mode == "wan":
            status, msg, perf = check_wan(modem, args)
        else:
            raise PluginError(f"modo sconosciuto: {args.mode}")
    except PluginError as e:
        print(f"UNKNOWN - {e}")
        sys.exit(UNKNOWN)
    except Exception as e:
        print(f"UNKNOWN - {type(e).__name__}: {e}")
        sys.exit(UNKNOWN)


    print(f"{LABEL[status]} - {msg}" + (f" | {perf}" if perf else ""))
    if args.mode == "hosts" and args.verbose:
        for row in table:
            print(row)

    sys.exit(status)


if __name__ == "__main__":
    main()

