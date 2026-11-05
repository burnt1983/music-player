# Music Player

A transparent music player for the Linux desktop. It plays songs from `~/Music`. The window itself is clear. Only the controls are drawn.

Five equalizer sliders (Bass, Low, Mid, High, Treble) drive a GStreamer equalizer. Gold is a boost. Blue is a cut. The sliders stay flat until you move one.

## Install

Needs Python 3, GTK 3, PyGObject, cairo, and GStreamer (`python3-gi`, `python3-gi-cairo`, `gir1.2-gtk-3.0`, `gir1.2-gst-plugins-base-1.0`, `gstreamer1.0-plugins-good`).

```bash
git clone https://github.com/burnt1983/music-player.git
cd music-player
bash install.sh
player
```

Place, volume, and equalizer settings are saved in `~/.config/lee-player/config.json`. It does not start at login until you tick Open at login. Playback stays closed until you press play.

## How to use it

| Action | What happens |
|---|---|
| Drag empty space | Move the window |
| Scroll a slider | Move that band by 1 dB |
| Double-click a slider | Return that band to zero |
| Scroll anywhere else | Volume |
| Drop a song or a folder | Play it |
| Right-click | Songs, shuffle, repeat, equalizer presets, stay above windows, open at login, quit |

Hidden folders are skipped, and so are `node_modules`, `venv`, and `Trash`.

## Uninstall

```bash
bash uninstall.sh
```

## Licence

MIT. See [LICENSE](LICENSE).
