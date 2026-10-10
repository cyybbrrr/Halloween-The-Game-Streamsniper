# Halloween: The Game Streamsniper

An always-on-top external panel that OCR-reads **only the bright text** on your screen. Everything darker than the
"Text brightness" setting (the background) is thrown away before OCR runs, so it never reads the background.

Streamsnipe your friends and have fun! I do not encourage witch-hunting with this program. I just saw it as a fun project.

**This is a TEST concept for a stream-sniper for Halloween: The Game. If you use this tool and get banned, I will not be responsible. It is your decision to run something that is still in testing.**

This program should work on all resolutions, please report bugs to me on Discord if something seems off. It is confirmed so far to fully work on 1440p displays.

## What it does

**Lobby watch (F8)** scans the region you pick for your keywords. If none are found for several scans in a row:

1. Scans the **entire screen** for `Leave Match` and clicks it.
2. 1 second later, scans the **center of the screen** for `Leave Match` and clicks it.
3. Waits **20 seconds (by default)**, scans the **entire screen** for `MATCHMAKE` and clicks it **once**.
4. Waits the "Resume watch" time (30 s by default) so it doesn't judge the queue screen as a bad lobby, then goes back to scanning.

**Start Streamsnipe** (button) waits **5 seconds**, scans the **entire screen** for `MATCHMAKE` and clicks it **once**.
Press it again while counting down to cancel.

If a button can't be found, nothing is clicked, and the status shows what was missing.

## Setup (Windows)

1. Install Python 3.10+ from python.org (tick "Add Python to PATH").
2. Install Tesseract OCR (UB Mannheim build) to the default location: `C:\Program Files\Tesseract-OCR\`
3. In this folder: `pip install -r requirements.txt`
4. Run it (use an administrator terminal if F8/F10 don't respond): `python overlay.py`

Run the game in **borderless / windowed** mode so the panel can sit on top of it.

## First-time tuning (do this before relying on it)

1. Add your keywords, then **Set scan region** and drag a box around the lobby text.
2. **Preview text mask** shows exactly what OCR sees (black = text, white = ignored). Your keywords should be
   clearly readable there. If text is showing as missing, lower or higher **Text brightness**; it might depend on your games brightness.
3. **Test OCR** shows the words read and which keywords matched.
4. **Test buttons (no click)** scans the full screen and reports where it found `Leave Match` and `MATCHMAKE`
   without clicking anything. Do this with each button on screen.

## Settings

| Settings | Meaning |
|---|---|
| Scan every (s) | How often the lobby region is scanned |
| Misses to leave | Consecutive no-match scans before leaving (higher = safer) |
| Min text chars | Less text than this (loading/blank) is not counted as a miss |
| Keyword fuzz | 1.0 = exact only; ~0.85 tolerates small OCR mistakes |
| Text brightness | Pixels at/above this (0-255) count as text, the rest is dropped |
| Confirm scan (s) | Delay between the first and second `Leave Match` click (1 s) |
| Matchmake after (s) | Wait after leaving before looking for `MATCHMAKE` (20 s) |
| Resume watch (s) | Pause after a full leave + matchmake before watching again (30 s) |

Button names are editable in the panel. Extra options live in `config.json` (created next to the script):
`streamsnipe_delay` (5), `find_attempts` (5 re-scans before giving up), `center_fraction` (0.5 = middle half of the
screen), `screen_scale` (raise to 1.5-2 if button text is small, slower), `min_conf`, `button_fuzzy`, `monitor`.

## Safety

- Slam the mouse into a screen corner to abort a click (pyautogui failsafe).
- F8 stops the lobby watch, F10 quits.
- It only reads screen pixels and sends normal mouse clicks. It does not touch the game's files or memory.
- Some games' anti-cheat flags simulated input. Check the game's rules before using it in online play.
