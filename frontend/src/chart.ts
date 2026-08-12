// Per-trader portfolio-value line chart. A thin uPlot wrapper that starts at a
// 1x1 canvas and syncs to its container once the DOM has laid out. The line and
// its flat fill are coloured green or red by whether the trader is up overall.

import uPlot, { type Options } from "uplot";
import "uplot/dist/uPlot.min.css";

import type { ChartPoint } from "./state";

const MIN_HEIGHT = 110;
const Y_AXIS_WIDTH = 62;
const FILL_ALPHA = 0.07;

// uPlot does not scale a caller-supplied font for hiDPI, so bake the ratio in.
const AXIS_FONT = `${Math.round(10 * (window.devicePixelRatio || 1))}px -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif`;

export class PortfolioChart {
  private plot: uPlot;
  private host: HTMLElement;

  constructor(host: HTMLElement) {
    this.host = host;

    const opts: Options = {
      width: 1,
      height: 1,
      pxAlign: false,
      cursor: { show: false },
      legend: { show: false },
      scales: {
        // A single point would otherwise auto-range to a span of years; show a
        // five-minute window until enough points arrive to tighten it.
        x: {
          time: true,
          range: (_u, min, max) => (min === max ? [min - 300, max + 30] : [min, max]),
        },
        y: {
          range: (_u, min, max) =>
            min === max
              ? [min - 100, max + 100]
              : [min - (max - min) * 0.1, max + (max - min) * 0.1],
        },
      },
      axes: [
        {
          stroke: () => getVar("--fg-dim") || "#5c636f",
          font: AXIS_FONT,
          size: 26,
          gap: 2,
          // Horizontal rules alone read the value; vertical ones only add noise.
          grid: { show: false },
          ticks: { show: false },
        },
        {
          stroke: () => getVar("--fg-dim") || "#5c636f",
          font: AXIS_FONT,
          size: Y_AXIS_WIDTH,
          gap: 6,
          grid: { stroke: () => getVar("--grid") || "#1b1e24", width: 1 },
          ticks: { show: false },
          values: (_u, splits) => splits.map(formatCompact),
        },
      ],
      series: [
        {},
        {
          stroke: (u) => trendColor(u),
          fill: (u) => withAlpha(trendColor(u), FILL_ALPHA),
          width: 1.5,
        },
      ],
    };
    this.plot = new uPlot(opts, [[], []], host);

    // Double-RAF lets the panel grid finish layout before we read dimensions.
    requestAnimationFrame(() => requestAnimationFrame(() => this.syncSize()));
    new ResizeObserver(() => this.syncSize()).observe(this.host);
    // Re-read the theme colours when the user toggles light/dark.
    window.addEventListener("themechange", () => this.plot.redraw());
  }

  update(points: ChartPoint[]): void {
    const xs = points.map((p) => p.t);
    const ys = points.map((p) => p.value);
    this.plot.setData([xs, ys]);
  }

  private syncSize(): void {
    const rect = this.host.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    this.plot.setSize({
      width: Math.floor(rect.width),
      height: Math.max(MIN_HEIGHT, Math.floor(rect.height)),
    });
  }
}

function isUp(u: uPlot): boolean {
  const ys = u.data[1] as number[];
  return ys.length > 1 ? ys[ys.length - 1] >= ys[0] : true;
}

function trendColor(u: uPlot): string {
  const up = isUp(u);
  return getVar(up ? "--up" : "--down") || (up ? "#4cae86" : "#d4636d");
}

function getVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Flat, single-stop fill — deliberately not a gradient.
function withAlpha(hex: string, alpha: number): string {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function formatCompact(n: number): string {
  // Accounts sit around $10k, so a tight range needs full figures to stay distinct.
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  return `$${Math.round(n).toLocaleString("en-US")}`;
}
