# AI2U ↔ AIBOND Bridge

Unofficial Windows-only Python bridge connecting the **AI2U** game (via an OpenAI-compatible custom endpoint mod) to the **AIBOND** desktop character chat.

> **Community project / experimental.** Not affiliated with the developers of AI2U or AIBOND. You must own/install both applications. This repository contains no game files, AIBOND files, paid models, or API credentials.

## Features

- Sends AI2U dialogue context to AIBOND and returns NPC dialogue in the JSON shape expected by the AI2U custom endpoint.
- Uses Chrome DevTools Protocol (CDP) to send messages without switching window focus and Windows UI Automation to read replies.
- `normal` and `hardcore` dialogue styles (the latter encourages rude/strong language).
- Experimental NPC movement/following, mood/expression and anger signals.
- Faster response polling and a short-lived cache to reduce duplicate submissions.
- **Text only**: no screenshots or voice generation are implemented by this bridge.

## Requirements

- Windows, Python 3.10+ (or another version compatible with `pywinauto`)
- AI2U with a compatible custom AI endpoint mod (tested by the author with **AI2U Custom AI Endpoint v5.7.0**)
- AIBOND desktop Steam app, with a visible character chat

## Installation

1. Download the repository as ZIP and extract it, or clone it.
2. Open PowerShell in the extracted directory and install the dependencies:

   ```powershell
   py -m pip install -r requirements.txt
   ```

3. Close AIBOND completely and start it with CDP enabled (adjust the path if your Steam library is elsewhere):

   ```powershell
   & "C:\Program Files (x86)\Steam\steamapps\common\Aibond\she-steam.exe" --remote-debugging-port=9222
   ```

4. Open the main AIBOND character chat.
5. Run `start_normal.cmd` or `start_hardcore.cmd`. Alternatively:

   ```powershell
   py .\aibond_ai2u_final.py --mode normal
   py .\aibond_ai2u_final.py --mode hardcore
   ```

6. In AI2U's custom endpoint mod settings (usually **F9**), set:

   | Setting | Value |
   |---|---|
   | Base URL | `http://127.0.0.1:8765/v1` |
   | Model | `aibond` |
   | API key | `local` (placeholder, not a real secret) |
   | Streaming | Off |

## Modes

- **Normal:** original character style plus experimental movement and mood signals.
- **Hardcore:** coarser language and faster anger escalation. It does **not** guarantee an attack or the activation of Yandere Mode.

To switch modes, stop the bridge with Ctrl+C and relaunch it.

## Limitations

- The game/mod ultimately decides which action, facial-expression, anger and attack commands are valid. Not all animations may play. There is no fully autonomous item interaction.
- Duplicate protection is best-effort and keyed to identical requests, not semantically equivalent ones.
- The bridge relies on AIBOND's current UI structure and process name; future app updates may break it.
- Speech must be configured in AIBOND or AI2U separately. The game's offline voice may not handle Russian correctly.
- This project does **not** grant AIBOND/PicoClaw access to Windows files, commands or administrator privileges.

## Safety and privacy

- The bridge listens on `127.0.0.1:8765` and uses CDP port `9222`. **Never expose these ports to the internet or untrusted networks.**
- Running AIBOND with remote debugging enables local programs to interact with its UI. Close AIBOND when done.
- Do not commit chat logs, API keys, saved games, personal configurations or credentials.
- Review and test the script locally before using it with accounts that have paid credits.

## License

No license has been selected yet. If you want others to legally reuse and modify the code, add an explicit license (e.g. MIT) after reviewing the third-party app terms and any borrowed code.
