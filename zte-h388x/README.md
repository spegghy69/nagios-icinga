# check_zte_h388x

Plugin Nagios/Icinga per il modem ZTE H388X (firmware AGZHP_1.4.4), che legge
via interfaccia web: stato della WAN (fibra/SFP), traffico delle porte LAN,
traffico e dispositivi Wi-Fi, host connessi.

## Requisiti

- Python 3.8 o superiore (solo libreria standard, nessuna dipendenza esterna)
- Accesso HTTP al modem dal server di monitoraggio
- Un utente con permessi di sola lettura sul modem (consigliato)

## Installazione

Con OMD, il plugin va nella cartella dei plugin del site:

```bash
cp check_zte_h388x.py /omd/sites/sito/local/lib/monitoring-plugins/check_zte_h388x
chmod 755 /omd/sites/sito/local/lib/monitoring-plugins/check_zte_h388x
```

## Credenziali

Il plugin legge utente e password da un file `KEY=VALUE`:

```
# /omd/sites/sito/etc/zte.env
ZTE_USER=user
ZTE_PASS=la_tua_password
```

Permessi: il file deve essere leggibile solo dall'utente del site.

```bash
chown sito:sito /omd/sites/sito/etc/zte.env
chmod 600 /omd/sites/sito/etc/zte.env
```

Il plugin si rifiuta di partire se il file è accessibile da altri utenti.

## File dei dispositivi autorizzati

Opzionale, usato dalla modalità `hosts`. Formato di ogni riga:

```
mac hostname   # commento
mac,hostname   # commento
mac            # senza hostname
```

Il commento dopo `#` viene ignorato. Esempio:

```
# dispositivi autorizzati
86:W9:1E:Q3:LF:B9   phone           # telefono
B8:27:EB:Z3:79:1W   berry           # raspberry
00:0C:HL:D4:D3:92   vmlinux         # VM
```

Regole:

- il MAC va in formato `xx:xx:xx:xx:xx:xx`, con i due punti;
- l'hostname non può contenere spazi (usa `-` o `_`);
- una riga con più di due campi o un MAC non valido fa fallire il check con
  l'indicazione del numero di riga.

Il nome risolto nella tabella è quello del file; se manca, il nome annunciato
dal dispositivo; se manca anche quello, `(nessun nome)`.

## Modalità

| Modalità  | Cosa controlla                                           |
|-----------|----------------------------------------------------------|
| `wan`     | stato del link WAN, velocità, potenza ottica RX/TX (dBm) |
| `traffic` | traffico delle porte LAN (kbit/s e contatori)            |
| `wifi`    | SSID attivi, traffico per rete e banda                   |
| `hosts`   | host connessi, sconosciuti per porta e SSID              |

## Uso

```bash
check_zte_h388x -H 192.168.1.1 -m wan --expect-speed 1000 -w -25 -c -28 --creds zte.env
check_zte_h388x -H 192.168.1.1 -m traffic -p LAN2,LAN4 -w 50000 --creds zte.env
check_zte_h388x -H 192.168.1.1 -m wifi --creds zte.env
check_zte_h388x -H 192.168.1.1 -m hosts -w 15 -c 25 --allowed-macs zte_macs.txt --creds zte.env
```

Opzioni principali:

| Opzione          | Significato                                          |
|------------------|------------------------------------------------------|
| `-H`             | IP del modem                                         |
| `-m`             | modalità (`wan`, `traffic`, `wifi`, `hosts`)         |
| `--creds`        | file con `ZTE_USER` e `ZTE_PASS`                     |
| `-p`             | porte LAN che devono essere `Up` (solo `traffic`)    |
| `--expect-speed` | velocità attesa in Mbps per la WAN                   |
| `-w` / `-c`      | soglie warning / critical                            |
| `--allowed-macs` | file dei dispositivi autorizzati (solo `hosts`)      |
| `-d`             | debug su stderr (solo test manuali)                  |

Con `-w` negativo (es. dBm), passa il valore con lo spazio: `-w -25`.

## Configurazione Icinga/OMD

Comandi in `zte_h388x.cfg`, con il percorso dei file in `resource.cfg`:

```cfg
define command {
    command_name    check_zte_wan
    command_line    $USER1$/check_zte_h388x -H $HOSTADDRESS$ -m wan --expect-speed 1000 -w -25 -c -28 --creds $USER3$/zte.env
}
```

Ogni check è un servizio su un host dedicato al modem. Il `check_interval`
consigliato è di 5 minuti per il traffico (il calcolo della velocità dipende
dall'intervallo tra due esecuzioni) e 10 per Wi-Fi e host.

## Formato dell'output

Prima riga: stato e riassunto, con le perfdata dopo `|`.

```
OK - 12 host connessi (6 WiFi, 6 LAN) [LAN2: 5 noti, 0 sconosciuti ; ...] | 'hosts'=12;15;25 ...
```

Le righe successive (solo `hosts`) contengono la tabella dei dispositivi, senza
bordi, con colonne allineate da spazi.

Perfdata:

- `c` dopo un valore indica un contatore cumulativo: serve al backend per calcolare
  la derivata (velocità);
- le etichette sono nomi semplici (lettere, cifre, `_`): così NagFlux le
  registra come serie distinte.

## Visualizzazione

Thruk non interpreta tag HTML nel long output e non garantisce un font
monospazio: la tabella dei dispositivi è leggibile nel terminale con `-v`, e
nel riassunto del servizio. Non usare il carattere `|` nel long output: Icinga
lo interpreta come separatore delle perfdata.

## Risoluzione dei problemi

- **`login rifiutato dal modem (login_need_refresh)`**: il login non è stato
  completato. Verifica le credenziali e che lo User-Agent non sia cambiato.
- **`sessione non valida (SessionTimeout)`**: la sessione non è legata alla
  pagina corretta. Il plugin apre prima la pagina del menu, come il browser.
- **`sezione ... assente nella risposta`**: il modem ha cambiato il formato
  della pagina dopo un aggiornamento del firmware. Confronta la risposta con
  quella del browser (F12 → Rete).
- **Contatori azzerati**: il modem è stato riavviato. Il campione viene scartato
  e la velocità riparte dal successivo.
