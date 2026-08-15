// The per-trader activity log — the panel's primary readout. Rows come from the
// backend already coloured by type (the same custom-tracing colours as the Gradio
// dashboard).

import type { LogRow } from "./api";

// How far from the bottom still counts as "following the stream".
const PIN_SLOP_PX = 24;

// Log rows are stamped by SQLite's datetime('now'), which is UTC — not the trading floor
// machine's clock, and not the transaction timestamps beside them, which Python writes in
// local time. Render them in IST so the panel reads as one wall clock.
const TIME_ZONE = "Asia/Kolkata";
const timeFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: TIME_ZONE,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

export class LogView {
  private host: HTMLElement;

  constructor(host: HTMLElement) {
    this.host = host;
    host.classList.add("log");
  }

  render(rows: LogRow[]): void {
    // The whole list is rebuilt every poll, which resets scrollTop. Follow the tail
    // only while the user is already at the bottom; if they have scrolled back to
    // read something, hold their position instead of yanking them forward.
    const wasPinned = this.isPinned();
    const previousTop = this.host.scrollTop;

    this.host.innerHTML = "";
    if (rows.length === 0) {
      const empty = document.createElement("div");
      empty.className = "log-empty";
      empty.textContent = "Waiting for activity";
      this.host.append(empty);
      return;
    }
    for (const row of rows) {
      const el = document.createElement("div");
      el.className = "log-row";

      const time = document.createElement("span");
      time.className = "log-time";
      time.textContent = timeOf(row.datetime);

      const type = document.createElement("span");
      type.className = "log-type";
      type.style.color = row.color;
      type.textContent = row.type;

      const text = document.createElement("span");
      text.className = "log-text";
      text.textContent = row.message;

      el.append(time, type, text);
      this.host.append(el);
    }

    this.host.scrollTop = wasPinned ? this.host.scrollHeight : previousTop;
  }

  private isPinned(): boolean {
    const distanceFromBottom =
      this.host.scrollHeight - this.host.clientHeight - this.host.scrollTop;
    // A fresh, unscrolled pane reports 0 here, so the first render pins as intended.
    return distanceFromBottom <= PIN_SLOP_PX;
  }
}

function timeOf(stamp: string): string {
  // Stored as "YYYY-MM-DD HH:MM:SS" with no zone marker, so Date would otherwise read it
  // as the viewer's local time. The "Z" is what makes it parse as the UTC it actually is.
  const utc = new Date(`${stamp.replace(" ", "T")}Z`);
  if (Number.isNaN(utc.getTime())) {
    // Unparseable stamp: show the raw clock field rather than "Invalid Date".
    const parts = stamp.split(" ");
    return parts.length > 1 ? parts[1] : stamp;
  }
  return timeFormat.format(utc);
}
