# ETS2/ATS Local Radio for Linux

A native Linux port of [ETS2/ATS Local Radio](https://github.com/Koenvh1/ets2-local-radio). It plays real local radio based on where you are driving in Euro Truck Simulator 2 or American Truck Simulator.

It works with:

- the **native Linux** version of ETS2/ATS
- the **Windows** version under **Proton**, including **TruckersMP** (Steam Proton, umu, tinkergame, TLM, etc.)

It uses the same web player, stations, cities and map coverage as the Windows version, all served from this repository's `web/` folder. Only the parts that couldn't run on Linux were rewritten.

| Windows version | Linux version |
|---|---|
| `ETS2 Local Radio server.exe` (WinForms, .NET) | `linux/server/ets2_local_radio.py`. Python 3 standard library only, nothing to `pip install` |
| `local-radio.dll` shared-memory plugin (unreadable from Linux when the game runs under Proton) | `local-radio-linux.dll` / `.so`. Tiny plugin that sends position over loopback UDP |
| Global keyboard hook + DirectInput controller | evdev: works on Wayland **and** X11, including wheel/controller buttons |
| Server window | Same window in Qt (`ets2-local-radio gui`): status, plugin install, click-and-press key / wheel bindings, options, live log. Without Qt it falls back to opening the website |
| In-game overlay (D3D hook) | Optional desktop notification (`"notify": true`) |

## Install

```bash
git clone --depth 1 -b linux https://github.com/KoJesko/ets2-local-radio.git
cd ets2-local-radio
./linux/install.sh            # add --autostart to run the server at login
```

The installer:

1. Builds the plugin if `g++` and `mingw-w64-gcc` are installed, otherwise uses the prebuilt binaries in `linux/plugin/prebuilt/`.
2. Links `ets2-local-radio` into `~/.local/bin`.
3. Adds an **ETS2 Local Radio** app-menu entry and a systemd user service.
4. Finds ETS2/ATS in your Steam libraries (native, Flatpak and Snap Steam) and copies the plugin into both `bin/win_x64/plugins` (Proton) and `bin/linux_x64/plugins` (native).

Then:

1. Open **ETS2 Local Radio** from the app menu. The server window opens and runs the server, the same as on Windows; closing it stops the server. Click **Open ETS2 Local Radio** to open the radio in your browser.
   The window needs PySide6 or PyQt6 (`sudo pacman -S pyside6`, `sudo apt install python3-pyside6.qtwidgets`, `sudo dnf install python3-pyside6`). Without it, the menu entry just opens the website, and the server keeps running in the background.
2. Start the game and accept the *"Advanced SDK features"* dialog.
3. Drive. Click once on the page so the browser allows audio playback.

The game isn't in a Steam library, or you use a non-Steam copy? Run:

```bash
ets2-local-radio install-plugin --path "/path/to/Euro Truck Simulator 2"
```

Uninstall with `./linux/install.sh --uninstall`. Your settings and favourites are kept.

## Usage

```
ets2-local-radio [serve] [--open] [--port N] [--host ADDR] [--no-input]   run the server
ets2-local-radio gui                        server window (status, bindings, plugin install, log)
ets2-local-radio open                       start the server if needed and open the page
ets2-local-radio status                     what the server is receiving from the game
ets2-local-radio ctl next|previous|stop|volume-up|volume-down|favourite|go-to-favourite
ets2-local-radio learn                      show key / controller button names for settings.json
ets2-local-radio install-plugin [--game ets2|ats] [--path DIR] [--uninstall]
```

### Phone / tablet remote

By default the server listens on all interfaces (`"host": "0.0.0.0"`), and on start it prints the LAN address to open on your phone. Remote control by PeerID works the same as on Windows. To keep the server on this PC only, set `"host": "127.0.0.1"`.

Like the Windows version, the server has no login: anyone on your LAN can open the page and change stations or favourites. Requests aimed at it from other websites (cross-site requests, DNS rebinding) are rejected.

## Settings

Easiest: use the **Keyboard**, **Controller** and **Options** tabs in the server window and press **Save**, which also restarts the server.

`~/.config/ets2-local-radio/settings.json` is created on first start. If you edit it by hand, restart the server afterwards (`systemctl --user restart ets2-local-radio`, if you use the service).

```json
{
  "language": "en-GB",
  "port": 8330,
  "host": "0.0.0.0",
  "udpPort": 8331,
  "controller": "MOZA R5",
  "notify": false,
  "keys":    { "next": "Next", "previous": "PageUp", "stop": "End",
               "volumeUp": "Oemplus", "volumeDown": "OemMinus",
               "makeFavourite": "Pause", "goToFavourite": "" },
  "buttons": { "next": "5", "previous": "4", "stop": "", "volumeUp": "",
               "volumeDown": "", "makeFavourite": "", "goToFavourite": "MOZA#12" }
}
```

- **The file format is the same as Windows.** You can copy `settings.json` and `favourites.json` over from a Windows install. Windows key names (`Next`, `Oemplus`, `D1`, `NumPad5`…) are understood, and so are Linux names (`KEY_PAGEDOWN`, `pagedown`).
- **Keys** are read from `/dev/input`, which needs your user in the `input` group (`sudo usermod -aG input $USER`, then log out and back in). Without that, bind desktop shortcuts to `ets2-local-radio ctl next` and so on instead.
- **Controller buttons** usually work without any extra group. Run `ets2-local-radio learn` and press the buttons on your wheel. It prints values like `"5"` (button 5 of `"controller"`) or `"MOZA R5 Base#5"` (button 5 of that device, whatever `"controller"` is set to). D-pad/hat directions come after the buttons: up, right, down, left. Button numbers match `jstest`.
- **`custom.js`**: put your custom stations and settings in `~/.config/ets2-local-radio/custom.js` instead of `web/custom.js`, so `git pull` never conflicts with them. See `web/custom.example.js`.
- **`udpPort`**: if you change it, also set `LOCAL_RADIO_UDP_PORT=<port>` in the game's environment (Steam launch options: `LOCAL_RADIO_UDP_PORT=9000 %command%`).

## In-game screen: CoachNowPlaying

The [Linux build of CoachNowPlaying](https://github.com/KoJesko/CoachNowPlaying/tree/linux) (the plugin for the
"CarPlay-Style Infotainment for Coaches" mod) shows Local Radio on the coach's screen. It asks this server which
station plays (`/nowplaying/`) and finds the song from the station's stream title, or with Shazam (SongRec) when the
station doesn't send one. The screen then alternates between the song and "%COUNTRY%'s best %GENRE% station". The screen's
next/previous buttons switch stations through `/control/`.

There are some special Easter Eggs for certain stations, typically found in map mods, but some are in the base game areas.

`GET /nowplaying/` returns `{"station", "logo", "signal", "since", "game"}` for the station the page last switched to.

## How it works

```
 ETS2/ATS (native, or Windows build under Proton/Wine)
   └─ local-radio-linux.(so|dll)   SCS telemetry plugin
        │  JSON datagram, 4×/s:  {"game":"ets2","x":..,"y":..,"z":..,"paused":false}
        ▼  udp://127.0.0.1:8331
 ets2_local_radio.py ───── http://0.0.0.0:8330 ─────▶ browser running web/ (unchanged upstream UI)
   ▲  /api/ (position) · /commands/ (SSE) · /favourite/ · /station/ · /eskago/ · /control/
   └─ evdev: keyboard hotkeys, wheel / controller buttons
```

The upstream plugin publishes telemetry through a Windows *named shared-memory mapping*. Under Proton that object exists only inside Wine, so a native Linux program cannot open it without a helper process running inside the game's prefix. Wine's sockets, on the other hand, are real Linux sockets. That's why the Linux plugin sends a small UDP datagram instead: the same source builds as a Windows DLL for Proton and as an `.so` for the native game, and nothing extra has to run inside the Wine prefix.

The server exposes the same `/api/` JSON shape that the upstream web page reads (`TruckValues.Positioning.HeadPositionInWorldSpace`). For that it uses the truck's world position, which is at most a few metres from the head position and makes no difference against the 7.5 km broadcast radius.

## Troubleshooting

- **The page says "Waiting for your in-game coordinates"**: run `ets2-local-radio status` while driving. If `sdk active` is `False`:
  - Check `Documents/<game>/game.log.txt` for `[local-radio-linux] sending telemetry to 127.0.0.1:8331`. Under Proton this file is in the prefix's `Documents`, or in your `-homedir`.
  - If that line is missing, the plugin isn't installed in the folder the game actually runs from. Re-run `install-plugin --path`.
- **Hotkeys don't do anything**: the server log (`journalctl --user -u ets2-local-radio -f`, or the terminal) shows which devices it watches and any permission problems.
- **Debug HTTP traffic**: run `LOCAL_RADIO_DEBUG=1 ets2-local-radio`.

## Development

```bash
make -C linux/plugin                 # build/local-radio-linux.dll + .so
# fake game: loads the plugin and "drives" around Berlin, without ETS2
g++ -Iplugin/scs_sdk/include -o /tmp/fake_game linux/plugin/test/fake_game.cpp -ldl
/tmp/fake_game linux/plugin/build/local-radio-linux.so 10343 79.88 -9903.56 30
# the same for the DLL under Wine
x86_64-w64-mingw32-g++ -static -Iplugin/scs_sdk/include -o /tmp/fake_game.exe linux/plugin/test/fake_game.cpp
wine /tmp/fake_game.exe local-radio-linux.dll 10343 79.88 -9903.56 30
```

Station, city and translation updates come from upstream. To merge them:

```bash
git remote add upstream https://github.com/Koenvh1/ets2-local-radio.git
git pull upstream master
```

The `linux/` folder is the only Linux-specific part.

## License

MPL-2.0, the same as upstream. The SCS SDK headers are © SCS Software (see `plugin/scs_sdk/sdk_license.txt`).
