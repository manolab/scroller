# Scroller

Gestisci da terminale i display LED a scorrimento basati su **CH583** (USB `0416:5020`, i badge LED a 11 righe
"LSLED / LS32"), senza l'app per smartphone. Si collega il display via USB e si modificano gli 8 messaggi
da un'interfaccia a menu.

Funziona su **macOS, Linux e Windows** ed è un unico file Python.

## Funzioni

- 8 messaggi: scegli quali far ciclare, in che ordine, con effetto (9 tipi), velocità, lampeggio e bordo animato
- **font diverso per ogni messaggio**:
  - un font pixel 5×7 integrato, pensato per i LED, con accenti italiani e lettere con le gambe (g, j, p, q, y);
  - qualsiasi font TrueType/OpenType del sistema, disegnato a pixel netti
- anteprima LED colorata direttamente nel terminale, aggiornata mentre scrivi
- avviso quando un messaggio con scorrimento in su/giù è più largo del display (la parte in eccesso non si vedrebbe)
- comandi rapidi con `/` (`/invia`, `/3 Buongiorno`, `/font 2`, `/larghezza 48`, …)
- rilevamento automatico del display, anche quando lo colleghi o scolleghi
- installazione automatica dei pacchetti Python mancanti

## Installazione

Serve Python 3.9 o successivo.

```sh
git clone https://github.com/manolab/scroller.git
cd scroller
python3 scroller.py
```

Al primo avvio, se mancano i pacchetti necessari, Scroller li installa da solo in un ambiente Python dedicato:

| pacchetto | a cosa serve |
|-----------|--------------|
| `hidapi`  | comunicazione USB (HID) con il display |
| `pillow`  | disegno dei caratteri con i font |

Se preferisci installarli tu: `pip install hidapi pillow`
(e avvia con `SCROLLER_NO_INSTALL=1` per disattivare l'installazione automatica).

Su Windows avvia con `py scroller.py`, possibilmente da Windows Terminal.

### Linux: permessi USB

Su Linux un utente normale non ha accesso al dispositivo. Esegui una volta:

```sh
echo 'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0416", ATTRS{idProduct}=="5020", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/99-scroller.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

poi scollega e ricollega il display.

## Uso

```sh
python3 scroller.py                    # interfaccia interattiva
python3 scroller.py invia              # invia al display i messaggi salvati
python3 scroller.py anteprima          # stampa l'anteprima dei messaggi nel ciclo
python3 scroller.py imposta 2 "Ciao"   # imposta il messaggio 2 e lo mette nel ciclo
python3 scroller.py controlla          # verifica pacchetti, display e permessi
```

### Tasti

| dove | tasto | azione |
|------|-------|--------|
| ovunque | `↑` `↓` · `enter` · `esc` | naviga · seleziona · indietro |
| ovunque | `←` `→` | cambia il valore selezionato |
| ovunque | `/` | comandi |
| ovunque | `ctrl+c` | esci |
| Messaggi | `spazio` | mette il messaggio nel ciclo o lo toglie |
| Messaggi | `e` · `f` | scrive il testo · sceglie il font |
| Messaggi | `⇧↑` `⇧↓` (Windows: `ctrl↑` `ctrl↓`) | sposta il messaggio |

### Larghezza del display

Questi badge hanno di solito 44 o 48 colonne. Scroller usa 44 come valore predefinito, e la larghezza serve
per gli avvisi sui messaggi troppo larghi. Per trovare quella giusta usa **Prova larghezza** nel menu: il
display mostra un rettangolo largo quanto il valore impostato.

- Se manca un lato verticale, riduci la larghezza.
- Se restano colonne spente ai lati, aumentala.

## Configurazione

I messaggi e le impostazioni vengono salvati in automatico in:

- macOS / Linux: `~/.config/scroller/config.json`
- Windows: `%APPDATA%\scroller\config.json`

## Protocollo

Il display si presenta come dispositivo HID vendor-defined, con report da 64 byte. I dati sono:

- un header di 64 byte che inizia con `wang`, con luminosità, lampeggio, bordo, effetto/velocità e lunghezza
  di ciascuno degli 8 messaggi;
- le bitmap dei messaggi, 11 byte per ogni blocco di 8 colonne (un byte per riga, bit più significativo a sinistra).

Il formato è lo stesso dei badge LED "LS32" documentati da altri progetti open source.

## Font extra

Puoi aggiungere altri font `.ttf`/`.otf` (i font "pixel" rendono meglio) in:

- macOS: `~/Library/Fonts`
- Linux: `~/.local/share/fonts`
- Windows: `%LOCALAPPDATA%\Microsoft\Windows\Fonts`
