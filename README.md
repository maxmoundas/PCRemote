# PC Remote

Control your Windows 11 PC from your iPhone over Wi‑Fi: trackpad, keyboard, and media controls in Safari. You don't need to install an app on the phone.

```
iPhone Safari  ──WebSocket (LAN)──▶  Python server on PC  ──SendInput──▶  Windows
 (web app)                            (aiohttp)                         (any app)
```

## Quick start

1. Install **Python 3.10+** from python.org (tick *Add python.exe to PATH*).
2. Double‑click **`start.bat`**. The first run creates a virtual environment and installs two small packages.
3. If Windows Firewall asks, allow access on **Private networks**.
4. On your iPhone, **scan the QR code** in the console with the Camera app. Or open the printed URL (e.g. `http://192.168.1.69:8765`) and type the 6‑digit PIN.
5. Optional: in Safari tap **Share → Add to Home Screen** to get a full‑screen app icon. The home‑screen app keeps its own storage, so you'll pair it once more.

The phone stays paired. Next time, start the server and open the page.

## Using it

| Tab | What it does |
|---|---|
| **Trackpad** | 1 finger moves the pointer; **tap** = left click; **2‑finger tap** = right click; **3‑finger tap** = middle click; **2‑finger swipe** = scroll with momentum; **press & hold, then move** = drag. The **Left/Right** buttons work like real mouse buttons: hold *Left* with your thumb and move with another finger to drag. The ⌨ button opens the keyboard. |
| **Keyboard** | Tap the field and type with the iOS keyboard. Text is sent as you type, and backspace, dictation and emoji all work. **Ctrl/Alt/Shift/Win** are sticky: tap one, then type a letter or tap a key. There are buttons for navigation keys and common shortcuts (copy/paste, Alt+Tab, close tab, Task View, etc.). Arrow, backspace and zoom keys repeat while held. |
| **Media** | Play/Pause, Prev/Next track and Volume use the Windows media keys, so they work with YouTube, Netflix, Spotify, VLC and others. **Seek** sends ←/→ (YouTube/Netflix/VLC skip). **Fullscreen** sends `F` (the player shortcut in YouTube, Netflix and VLC). **F11** makes a browser window fullscreen. Volume buttons repeat while held. A small trackpad is included so you can click without switching tabs. |

**Settings** (gear icon) has pointer speed, scroll speed, natural scrolling, and *Unpair this device*.

## How it works / design decisions

- **Input injection:** the server calls Win32 `SendInput` through `ctypes`, which is the same path a physical mouse and keyboard use. That's why it works in every app, not only browsers. Text goes through `KEYEVENTF_UNICODE`, so any character types correctly whatever the keyboard layout.
- **Latency:** there is one persistent WebSocket with `TCP_NODELAY`. Pointer and scroll deltas are collected and sent once per animation frame, so the socket never floods and the cursor tracks your finger. Buttons fire on touch‑down, not on click.
- **Reconnection:** a heartbeat every 2 s detects dead connections. When Safari comes back to the foreground, the page reconnects at once (iOS suspends background pages). Otherwise it retries with backoff, capped at 3 s. If the connection drops while a mouse button is held, the server releases it.
- **iOS typing:** the text field holds two invisible sentinel characters. After every `input` event the page compares the field's old and new contents and sends backspaces plus the inserted text. This copes with autocorrect, dictation, held backspace and composed input, which per‑key events miss on iOS.

### Security

- **Pairing:** a one‑time 6‑digit PIN is shown only on the PC. Pairing gives the phone a random 256‑bit token, which it keeps in local storage. The server stores only a SHA‑256 hash of the token, in `data/devices.json`.
- **PIN rotation:** the PIN changes after every successful pairing and after 5 wrong guesses. Each wrong guess also waits 1 s, so brute force is impractical.
- **WebSocket auth:** every connection must present a valid token within 5 s or it's closed.
- **LAN only:** requests from non‑private IP addresses are refused.
- **No HTTPS:** traffic is unencrypted on your local network. iOS makes self‑signed certificates painful, and for a home LAN this is a reasonable trade‑off. Don't run it on untrusted public Wi‑Fi.

Manage devices:

```
start.bat --list-devices
start.bat --forget-devices      # revoke every phone
```

## Options

```
start.bat --port 8765           # default port
start.bat --ip 192.168.1.50     # advertise a specific address in the URL/QR (e.g. if a VPN adapter is picked)
start.bat --no-qr
```

**Start with Windows:** press `Win+R`, type `shell:startup`, and put a shortcut to `start.bat` in that folder.

## Limitations

- **Admin windows:** Windows blocks injected input into apps running as Administrator (Task Manager, some installers and games) unless PC Remote is also run as Administrator. The UAC prompt and the lock screen can't be controlled, by design.
- **Pointer acceleration:** cursor motion goes through Windows' *Enhance pointer precision* setting, the same as a real mouse. Adjust pointer speed in Settings.
- **Pairing per app:** Safari and a Home Screen web app keep separate storage, so each needs pairing once.
- **Can't connect:** check that the phone and PC are on the same network and that the firewall allows Python on Private networks. Some router "guest" or "AP isolation" modes block device‑to‑device traffic.

## Project layout

```
pcremote/
  server.py      HTTP + WebSocket server, pairing/auth, message handling
  winput.py      ctypes SendInput wrapper (mouse, keys, unicode text)
  static/        the iPhone web app (index.html, app.js, style.css, icons)
start.bat        one-click launcher (creates .venv on first run)
requirements.txt aiohttp, qrcode
```
