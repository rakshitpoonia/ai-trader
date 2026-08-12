// The per-trader activity log — the panel's primary readout. Rows come from the
// backend already coloured by type (the same custom-tracing colours as the Gradio
// dashboard).

import type { LogRow } from "./api";

// How far from the bottom still counts as "following the stream".
const PIN_SLOP_PX = 24;

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
  // Stored as "YYYY-MM-DD HH:MM:SS"; show just the time.
  const parts = stamp.split(" ");
  return parts.length > 1 ? parts[1] : stamp;
}
