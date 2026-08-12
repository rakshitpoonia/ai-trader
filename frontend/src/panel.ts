// One trader's quadrant: header with value and profit, chart, holdings, log.

import type { LogRow } from "./api";
import { PortfolioChart } from "./chart";
import { Heatmap } from "./heatmap";
import { LogView } from "./log";
import type { TraderState } from "./state";
import { TransactionsView } from "./transactions";

export class TraderPanel {
  readonly root: HTMLElement;
  private state: TraderState;
  private chart: PortfolioChart | null = null;
  private heatmap: Heatmap;
  private log: LogView;
  private transactions: TransactionsView;
  private valueEl: HTMLElement;
  private deltaEl: HTMLElement;
  private pnlEl: HTMLElement;
  private pctEl: HTMLElement;
  private strategyEl: HTMLElement;

  constructor(state: TraderState) {
    this.state = state;
    const { name, model_name, lastname } = state.info;
    this.root = document.createElement("section");
    this.root.className = "panel";
    this.root.innerHTML = `
      <header class="panel-head">
        <div class="panel-id">
          <span class="panel-name"></span>
          <span class="panel-model"></span>
          <span class="panel-flag">Leader</span>
        </div>
        <div class="panel-figures">
          <span class="panel-value">$0</span>
          <span class="panel-delta">
            <span class="panel-pnl"></span>
            <span class="panel-pct"></span>
          </span>
        </div>
        <p class="panel-strategy"></p>
      </header>
      <div class="panel-chart"></div>
      <div class="panel-heatmap"></div>
      <div class="panel-bottom">
        <div class="panel-col primary">
          <span class="panel-col-label">Activity</span>
          <div class="panel-log"></div>
        </div>
        <div class="panel-col">
          <span class="panel-col-label">Recent trades</span>
          <div class="panel-transactions"></div>
        </div>
      </div>
    `;
    this.root.querySelector(".panel-name")!.textContent = name;
    this.root.querySelector(".panel-model")!.textContent = `${lastname} · ${model_name}`;
    this.valueEl = this.root.querySelector(".panel-value")!;
    this.deltaEl = this.root.querySelector(".panel-delta")!;
    this.pnlEl = this.root.querySelector(".panel-pnl")!;
    this.pctEl = this.root.querySelector(".panel-pct")!;
    this.strategyEl = this.root.querySelector(".panel-strategy")!;
    this.heatmap = new Heatmap(this.root.querySelector(".panel-heatmap")!);
    this.log = new LogView(this.root.querySelector(".panel-log")!);
    this.transactions = new TransactionsView(this.root.querySelector(".panel-transactions")!);
    // Chart is created in mount(), after the panel is in the DOM, because uPlot misbehaves
    // when its host is not laid out at construction time.
  }

  mount(): void {
    if (this.chart) return;
    this.chart = new PortfolioChart(this.root.querySelector(".panel-chart") as HTMLElement);
  }

  update(): void {
    const detail = this.state.detail;
    if (detail) {
      this.valueEl.textContent = formatMoney(detail.portfolio_value);
      this.deltaEl.dataset.trend = detail.pnl >= 0 ? "up" : "down";
      this.pnlEl.textContent = formatPnl(detail.pnl);
      // Everything the trader started with, backed out of the current value.
      const initial = detail.portfolio_value - detail.pnl;
      this.pctEl.textContent = initial > 0 ? formatPct((detail.pnl / initial) * 100) : "";
      this.heatmap.render(detail.holdings, this.state.priceDirections());
      this.state.rememberPrices();
      const strategy = detail.strategy.trim();
      this.strategyEl.textContent = strategy || "No strategy set yet";
      this.strategyEl.title = strategy;
      this.strategyEl.classList.toggle("empty", !strategy);
      this.transactions.render(detail.transactions);
    }
    this.chart?.update(this.state.chart);
  }

  renderLogs(rows: LogRow[]): void {
    this.log.render(rows);
  }

  setLeader(isLeader: boolean): void {
    if (isLeader) this.root.dataset.leader = "true";
    else delete this.root.dataset.leader;
  }
}

function formatMoney(n: number): string {
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}

function formatPnl(n: number): string {
  const sign = n >= 0 ? "+" : "-";
  return `${sign}${Math.abs(n).toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 })}`;
}

function formatPct(n: number): string {
  return `(${n >= 0 ? "+" : ""}${n.toFixed(2)}%)`;
}
