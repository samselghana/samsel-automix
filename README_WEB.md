# SAMSEL DJ Engine Pro – Web App

Full-featured web interface that replicates all desktop GUI controls from `dj_gui_pro.py`.

## Setup

```bash
pip install -r requirements.txt
```

## Run

### Local / LAN (all devices on your network)

```bash
python app.py
```

Or:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

- **This machine**: http://localhost:8000  
- **Other devices on LAN**: http://YOUR_LOCAL_IP:8000 (e.g. http://192.168.1.10:8000)

### Internet (share with anyone, any platform)

Install **cloudflared** or **ngrok** (one is enough):

| Platform | Install |
|----------|---------|
| Windows | `winget install cloudflare.cloudflared` or [download](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/) |
| macOS | `brew install cloudflared` |
| Linux | `sudo apt install cloudflared` or [docs](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/) |

Then run:

```bash
python run_public.py
```

Or: `python app.py --tunnel`

Or double-click `run_public.bat` (Windows) / `./run_public.sh` (Mac/Linux).

A public HTTPS URL will be printed (e.g. `https://xxxx.trycloudflare.com`). Share it to access the app from any device, anywhere.

**If uploads fail** ("context canceled" or timeouts): Cloudflare quick tunnels can drop large/slow uploads. Try:
- Use **ngrok** instead: `ngrok http 8000`, then open the ngrok URL
- Use **LAN access** for large files (open `http://YOUR_IP:8000` from devices on your network)
- Prefer smaller audio files when using the tunnel

## Features (mirrors desktop)

### Per deck (A & B)
- **Load File** – Single audio file
- **Load Folder** – Folder picker → playlist
- **Playlist** – Prev/Next, click to select, Reload Deck
- **Waveform** – Left-click seek, left-drag loop, right-click set hot cue
- **Play / Stop** – Transport
- **Sync to Other / Unsync** – BPM sync
- **Align to Other** – Beat alignment
- **Drop Sync** – Drop-sync transition
- **Mute / Quantize** – Toggles
- **Gain** – Slider
- **Seek** – Seconds + Go
- **Loop** – Beats (1–32), Loop On/Off
- **Roll** – Beats (1–16), Roll On/Off
- **Hot Cues** – Set/Go 1–8

### Center mixer
- **Load A / Load B** – Quick load
- **Start / Stop Engine**
- **Crossfader** – A ↔ B
- **Master Gain**
- **Auto DJ** – On/Off
- **Drop B into A / Drop A into B**
- **Align B to A / Align A to B**

### Engine
- Real-time status log
- Auto-advance playlist when track ends
- **Audio stream** – When using the app on a phone/tablet (LAN or tunnel), click **Listen on this device** to hear the mixed output in your browser. Audio plays on the device instead of the server PC. Expect 200–500 ms latency.

## Audio formats

MP3, WAV, FLAC, OGG, M4A, AAC

## Architecture

- **Backend**: FastAPI + `dj_engine_pro` (same engine as desktop)
- **Frontend**: HTML/CSS/JS with canvas waveform
- **Audio**: Playback via `sounddevice` (same as desktop)
