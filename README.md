<div align="center">

![TempOtoc](logo.png)

**Your time, told.**

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-FFDD5C?style=for-the-badge&logo=buymeacoffee&logoColor=white)](https://www.buymeacoffee.com/hugues_mellanger)

</div>

## The project

TempOtoc is a small desktop companion that takes care of a simple question: *where does your day go?*

It runs quietly in the background. When you sit down to work, you press a button and say in one sentence what you're starting to do. When you stop, you press again, and you say what happened. In the evening, the weekend, the month, the year: TempOtoc shows you the map of your time, period by period, with your comments written next to each slice.

No automatic tracking, no analysis of what you do on your machine: TempOtoc only notes what you tell it. It's a journal, not a cop.

## What it does

- **Clock in, in one sentence.** A "Start" button with a comment, a "Finish" button with a comment. The end comment is optional.
- **See your time.** A dashboard with four views: Day (the list of periods, minute by minute), Week, Month, Year (charts and totals). Idle periods appear too — the time between two sessions is visible, not hidden.
- **Filter.** A search field in the Day view selects the periods whose start comment contains a word — for example all "restarting" sessions, and the statistics are recomputed on the filter.
- **Edit.** Every comment is editable directly in the list, including the ones on idle periods.
- **Delete.** An active period, a note, or the end of an idle period can be removed from the journal.
- **Rhythm of the day.** An hourly chart shows where your sessions sit in the day, and the totals accumulate per period.
- **Light or dark theme**, according to the saved preference.
- **Autostart.** TempOtoc launches with Windows, in silence, and shuts down cleanly when asked.

## How to use it

1. Download `Tempotoc.exe` from the [v1.0.0 release](https://github.com/hmellanger/TempOtoc/releases/download/v1.0.0/Tempotoc.exe).
2. Open the dashboard: it is served at `http://127.0.0.1:8765`.
3. At the start of a task: "Start" + a sentence. At the end: "Finish" + a sentence (optional).
4. Browse your Day / Week / Month / Year views. Filter, edit, delete.

The journal is a text file (`activity_log.jsonl`) — readable, editable, backupable. Your data stays on your machine.

## Under the hood

A single Python application, compiled into one self-contained `.exe` (PyInstaller). A small local HTTP server serves the dashboard; the journal is a simple JSON file, line by line. No database, no cloud, no telemetry.

## License

Licensed under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0) — see the [LICENSE](LICENSE) file for details.

---

*Made with coffee and minute timers. TempOtoc — your time, told.*
