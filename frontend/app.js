const generationChartContext = document.getElementById("generationChart");
const trendChartContext = document.getElementById("trendChart");
const chargingForm = document.getElementById("chargingForm");
const refreshButton = document.getElementById("refreshNowButton");

const SOURCE_META = {
  coal: { label: "Coal", color: "#f97316", icon: "&#128293;" },
  gas: { label: "Gas", color: "#fb7185", icon: "&#9981;" },
  solar: { label: "Solar", color: "#facc15", icon: "&#9728;" },
  wind: { label: "Wind", color: "#10b981", icon: "&#127788;" },
  hydro: { label: "Hydro", color: "#38bdf8", icon: "&#128167;" },
  nuclear: { label: "Nuclear", color: "#c084fc", icon: "&#9762;" },
};

const state = {
  generationChart: null,
  trendChart: null,
  refreshTimer: null,
  refreshInFlight: false,
  queuedForceRefresh: false,
  selectedSource: null,
  hoveredSource: null,
  lastGridTimestamp: null,
  latestGeneration: null,
};

const recommendedMarkerPlugin = {
  id: "recommendedMarker",
  afterDatasetsDraw(chart, args, options) {
    if (!options?.timestamps?.length || !options.timestamp) {
      return;
    }

    const target = new Date(options.timestamp).getTime();
    let bestIndex = -1;
    let bestDelta = Number.POSITIVE_INFINITY;
    options.timestamps.forEach((timestamp, index) => {
      const delta = Math.abs(new Date(timestamp).getTime() - target);
      if (delta < bestDelta) {
        bestDelta = delta;
        bestIndex = index;
      }
    });

    if (bestIndex < 0) {
      return;
    }

    const { ctx, chartArea, scales } = chart;
    const x = scales.x.getPixelForValue(bestIndex);
    ctx.save();
    ctx.strokeStyle = options.color || "#71f7b2";
    ctx.lineWidth = 2;
    ctx.setLineDash([5, 5]);
    ctx.beginPath();
    ctx.moveTo(x, chartArea.top + 6);
    ctx.lineTo(x, chartArea.bottom);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = options.color || "#71f7b2";
    ctx.font = "12px 'IBM Plex Sans'";
    ctx.fillText("Recommended start", Math.min(x + 8, chartArea.right - 120), chartArea.top + 16);
    ctx.restore();
  },
};

Chart.register(recommendedMarkerPlugin);

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || `Failed to fetch ${url}`);
  }
  return response.json();
}

function getElement(id) {
  return document.getElementById(id);
}

function setText(id, value) {
  const element = getElement(id);
  if (element) {
    element.textContent = value;
  }
}

function setHtml(id, value) {
  const element = getElement(id);
  if (element) {
    element.innerHTML = value;
  }
}

function setClassName(id, value) {
  const element = getElement(id);
  if (element) {
    element.className = value;
  }
}

function setLoadingState(isLoading) {
  const shell = getElement("dashboardShell");
  if (shell) {
    shell.dataset.loading = isLoading ? "true" : "false";
  }
}

function setPillClass(id, tone) {
  const base =
    "inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em]";
  const toneClass =
    tone === "live"
      ? " status-pill-live"
      : tone === "cached"
        ? " status-pill-cached"
        : " status-pill-offline";
  setClassName(id, `${base}${toneClass}`);
}

function flashPanel(id) {
  const element = getElement(id);
  if (!element) {
    return;
  }
  element.classList.remove("update-flash");
  requestAnimationFrame(() => element.classList.add("update-flash"));
}

function totalGeneration(generation) {
  return Object.values(generation).reduce((sum, value) => sum + value, 0);
}

function renewableShare(generation) {
  const total = totalGeneration(generation);
  if (!total) {
    return 0;
  }
  return ((generation.solar + generation.wind + generation.hydro) / total) * 100;
}

function dominantSourceEntry(generation) {
  return Object.entries(generation).sort((left, right) => right[1] - left[1])[0] || ["coal", 0];
}

function formatNumber(value, maximumFractionDigits = 0) {
  return new Intl.NumberFormat("en-IN", { maximumFractionDigits }).format(value);
}

function hexToRgba(hex, alpha) {
  const normalized = hex.replace("#", "");
  const bigint = parseInt(normalized, 16);
  const r = (bigint >> 16) & 255;
  const g = (bigint >> 8) & 255;
  const b = bigint & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function formatDateTime(value) {
  return new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatShortTime(value) {
  return new Intl.DateTimeFormat("en-IN", {
    hour: "numeric",
    minute: "2-digit",
    month: "short",
    day: "numeric",
  }).format(new Date(value));
}

function formatDelayMinutes(timestamp) {
  const diff = Math.max(0, Math.round((Date.now() - new Date(timestamp).getTime()) / 60000));
  return diff === 0 ? "Live" : `${diff} min`;
}

function animateNumber(id, target, formatter, duration = 650) {
  const element = getElement(id);
  if (!element) {
    return;
  }

  const startValue = Number(element.dataset.currentValue || 0);
  const endValue = Number(target || 0);
  const startedAt = performance.now();

  function step(now) {
    const progress = Math.min((now - startedAt) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3);
    const current = startValue + (endValue - startValue) * eased;
    element.textContent = formatter(current);
    if (progress < 1) {
      requestAnimationFrame(step);
    } else {
      element.dataset.currentValue = String(endValue);
      element.textContent = formatter(endValue);
    }
  }

  requestAnimationFrame(step);
}

function getCarbonBand(value) {
  if (value <= 380) {
    return {
      label: "Low Carbon",
      badgeClass: "inline-flex rounded-full border border-emerald-400/30 bg-emerald-500/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-emerald-200",
    };
  }
  if (value <= 560) {
    return {
      label: "Moderate Carbon",
      badgeClass: "inline-flex rounded-full border border-amber-400/30 bg-amber-500/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-amber-200",
    };
  }
  return {
    label: "High Carbon",
    badgeClass: "inline-flex rounded-full border border-rose-400/30 bg-rose-500/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-rose-200",
  };
}

function getTrendInfo(history, currentValue) {
  const previousPoint = history?.length >= 2 ? history[history.length - 2] : null;
  if (!previousPoint) {
    return { label: "Trend pending", direction: "flat" };
  }

  const delta = currentValue - previousPoint.carbon_intensity;
  if (delta > 2) {
    return { label: `↑ Increasing (${delta.toFixed(1)})`, direction: "up" };
  }
  if (delta < -2) {
    return { label: `↓ Improving (${Math.abs(delta).toFixed(1)})`, direction: "down" };
  }
  return { label: "→ Stable", direction: "flat" };
}

function setRingProgress(percent) {
  const ring = getElement("carbonRingProgress");
  if (!ring) {
    return;
  }
  const circumference = 2 * Math.PI * 54;
  ring.style.strokeDashoffset = `${circumference * (1 - percent / 100)}`;
}

function renderGenerationChart(generation) {
  if (!generationChartContext) {
    return;
  }

  state.latestGeneration = generation;
  const focusSource = state.hoveredSource || state.selectedSource;
  const labels = Object.keys(SOURCE_META).map((source) => SOURCE_META[source].label);
  const values = Object.keys(SOURCE_META).map((source) => generation[source] || 0);
  const colors = Object.keys(SOURCE_META).map((source) => {
    const color = SOURCE_META[source].color;
    return !focusSource || focusSource === source ? color : hexToRgba(color, 0.18);
  });

  if (!state.generationChart) {
    state.generationChart = new Chart(generationChartContext, {
      type: "pie",
      data: {
        labels,
        datasets: [
          {
            data: values,
            backgroundColor: colors,
            borderColor: "#0f172a",
            borderWidth: 2,
            hoverOffset: 10,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 650, easing: "easeOutQuart" },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label(context) {
                const total = context.dataset.data.reduce((sum, value) => sum + value, 0);
                const share = total ? (context.raw / total) * 100 : 0;
                return `${context.label}: ${formatNumber(context.raw)} MW (${share.toFixed(1)}%)`;
              },
            },
          },
        },
      },
    });
  } else {
    state.generationChart.data.labels = labels;
    state.generationChart.data.datasets[0].data = values;
    state.generationChart.data.datasets[0].backgroundColor = colors;
    state.generationChart.update();
  }
}

function renderEnergySourceCards(generation) {
  const container = getElement("energyMetricsGrid");
  if (!container) {
    return;
  }

  const total = totalGeneration(generation);
  const focusSource = state.hoveredSource || state.selectedSource;
  const [dominantSource, dominantValue] = dominantSourceEntry(generation);
  if (!state.selectedSource) {
    const dominantShare = total ? (dominantValue / total) * 100 : 0;
    setText(
      "generationInsight",
      `${SOURCE_META[dominantSource].label} is currently leading the stack at ${dominantShare.toFixed(1)}% of total generation.`,
    );
  }
  container.innerHTML = Object.entries(SOURCE_META)
    .map(([source, meta]) => {
      const value = generation[source] || 0;
      const share = total ? (value / total) * 100 : 0;
      const active = focusSource === source;
      const muted = focusSource && focusSource !== source;
      return `
        <button type="button" data-source="${source}" class="energy-card ${active ? "active" : ""} ${muted ? "muted" : ""} p-4 text-left" style="--energy-color:${meta.color}">
          <div class="flex items-start justify-between gap-3">
            <div>
              <p class="text-xs uppercase tracking-[0.2em] text-slate-500">${meta.label}</p>
              <p class="mt-1 text-lg font-semibold text-slate-100">${meta.icon} ${share.toFixed(1)}%</p>
            </div>
            <span class="rounded-full px-2 py-1 text-xs font-semibold uppercase tracking-[0.12em]" style="background:${hexToRgba(meta.color, 0.12)}; color:${meta.color}">
              ${state.selectedSource === source ? "Isolated" : "Live"}
            </span>
          </div>
          <p class="mt-4 display-font text-2xl font-bold text-white">${formatNumber(value)} MW</p>
          <div class="energy-progress mt-4">
            <span style="width:${Math.max(share, 4)}%; background:linear-gradient(90deg, ${hexToRgba(meta.color, 0.68)}, ${meta.color});"></span>
          </div>
        </button>
      `;
    })
    .join("");

  container.querySelectorAll("[data-source]").forEach((button) => {
    button.addEventListener("mouseenter", () => {
      state.hoveredSource = button.dataset.source;
      renderGenerationChart(state.latestGeneration);
      renderEnergySourceCards(state.latestGeneration);
    });
    button.addEventListener("mouseleave", () => {
      state.hoveredSource = null;
      renderGenerationChart(state.latestGeneration);
      renderEnergySourceCards(state.latestGeneration);
    });
    button.addEventListener("click", () => {
      state.selectedSource = state.selectedSource === button.dataset.source ? null : button.dataset.source;
      renderGenerationChart(state.latestGeneration);
      renderEnergySourceCards(state.latestGeneration);
      setText(
        "generationInsight",
        state.selectedSource
          ? `${SOURCE_META[state.selectedSource].label} isolated in the chart. Click again to restore the full stack.`
          : "Hover a source card to spotlight it in the chart. Click to isolate a source.",
      );
    });
  });
}

function renderTrendChart(history, forecast, recommendation) {
  if (!trendChartContext) {
    return;
  }

  const historicalLabels = history.map((point) => formatShortTime(point.timestamp));
  const historicalData = history.map((point) => point.carbon_intensity);
  const forecastLabels = forecast.map((point) => formatShortTime(point.timestamp));
  const forecastData = forecast.map((point) => point.carbon_intensity);
  const timestamps = [...history.map((point) => point.timestamp), ...forecast.map((point) => point.timestamp)];
  const labels = [...historicalLabels, ...forecastLabels];
  const joinedHistory = [...historicalData, ...Array(forecast.length).fill(null)];
  const joinedForecast = [...Array(history.length).fill(null), ...forecastData];

  const gradientFill = (chart) => {
    const gradient = chart.ctx.createLinearGradient(0, 0, 0, chart.chartArea.bottom);
    gradient.addColorStop(0, "rgba(34, 211, 238, 0.28)");
    gradient.addColorStop(1, "rgba(34, 211, 238, 0.02)");
    return gradient;
  };

  if (!state.trendChart) {
    state.trendChart = new Chart(trendChartContext, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Historical carbon intensity",
            data: joinedHistory,
            borderColor: "#22d3ee",
            backgroundColor: (context) => (context.chart.chartArea ? gradientFill(context.chart) : "rgba(34, 211, 238, 0.18)"),
            fill: true,
            borderWidth: 3,
            tension: 0.38,
            pointRadius: 2.5,
            pointHoverRadius: 5,
          },
          {
            label: "Forecast carbon intensity",
            data: joinedForecast,
            borderColor: "#34d399",
            borderDash: [8, 6],
            borderWidth: 3,
            tension: 0.38,
            pointRadius: 2.5,
            pointHoverRadius: 5,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        animation: { duration: 700, easing: "easeOutQuart" },
        scales: {
          x: {
            ticks: { color: "#cbd5e1", maxRotation: 0, autoSkip: true },
            grid: { color: "rgba(148, 163, 184, 0.08)" },
          },
          y: {
            ticks: {
              color: "#cbd5e1",
              callback: (value) => `${value} g`,
            },
            grid: { color: "rgba(148, 163, 184, 0.08)" },
          },
        },
        plugins: {
          legend: {
            labels: {
              color: "#e2e8f0",
              font: { family: "IBM Plex Sans" },
            },
          },
          tooltip: {
            callbacks: {
              title(items) {
                return formatDateTime(timestamps[items[0].dataIndex]);
              },
              label(context) {
                return `${context.dataset.label}: ${context.raw.toFixed(2)} gCO2/kWh`;
              },
            },
          },
          recommendedMarker: {
            timestamp: recommendation.start_time,
            timestamps,
            color: "#71f7b2",
          },
        },
      },
    });
  } else {
    state.trendChart.data.labels = labels;
    state.trendChart.data.datasets[0].data = joinedHistory;
    state.trendChart.data.datasets[1].data = joinedForecast;
    state.trendChart.options.plugins.recommendedMarker = {
      timestamp: recommendation.start_time,
      timestamps,
      color: "#71f7b2",
    };
    state.trendChart.update();
  }
}

function renderCurrent(grid, carbon, recommendation, history, forecast) {
  setLoadingState(false);
  const total = totalGeneration(grid.generation);
  const renewable = renewableShare(grid.generation);
  const chargeNowKg = ((recommendation.energy_needed_kwh || 0) * carbon.carbon_intensity) / 1000;
  const durationHours = recommendation.duration_hours || 0;
  const expectedCarbon = recommendation.expected_carbon || carbon.carbon_intensity || 0;

  setText("updatedAt", formatDateTime(grid.timestamp));
  setText("statusMessage", grid.message || "Auto-refresh runs every minute.");
  animateNumber("carbonIntensityValue", carbon.carbon_intensity, (value) => value.toFixed(2));
  animateNumber("carbonRingPercent", Math.min(100, (carbon.carbon_intensity / 800) * 100), (value) => `${Math.round(value)}%`);
  setRingProgress(Math.min(100, (carbon.carbon_intensity / 800) * 100));

  setText("demandValue", `Demand: ${formatNumber(grid.demand)} MW`);
  setText("generationValue", `Generation: ${formatNumber(total)} MW`);
  animateNumber("heroDemandValue", grid.demand, (value) => `${formatNumber(value)} MW`);
  animateNumber("heroGenerationValue", total, (value) => `${formatNumber(value)} MW`);
  animateNumber("heroRenewableValue", renewable, (value) => `${value.toFixed(1)}%`);
  animateNumber("heroChargeNowValue", chargeNowKg, (value) => `${value.toFixed(2)} kg`);
  setText("heroDemandNote", `${formatDateTime(grid.timestamp)} snapshot`);
  setText("heroRenewableNote", "Solar, wind, and hydro mix");
  setText("heroDelayValue", formatDelayMinutes(grid.timestamp));
  setText("heroChargeNowNote", "Estimated CO2 if you charge immediately");

  const band = getCarbonBand(carbon.carbon_intensity);
  setText("carbonBandBadge", band.label);
  setClassName("carbonBandBadge", band.badgeClass);
  setText("carbonStatusBadge", band.label);
  setClassName("carbonStatusBadge", band.badgeClass);

  const trend = getTrendInfo(history, carbon.carbon_intensity);
  setText("carbonTrendBadge", trend.label);
  setClassName(
    "carbonTrendBadge",
    trend.direction === "up"
      ? "inline-flex rounded-full border border-rose-400/30 bg-rose-500/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-rose-200"
      : trend.direction === "down"
        ? "inline-flex rounded-full border border-emerald-400/30 bg-emerald-500/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-emerald-200"
        : "inline-flex rounded-full border border-slate-700 bg-slate-900/70 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-slate-300",
  );

  setText("chargingWindow", `${formatShortTime(recommendation.start_time)} - ${formatShortTime(recommendation.end_time)}`);
  setText("chargingNote", recommendation.message || "Waiting for forecast data.");
  setText("chargingBadge", recommendation.carbon_savings_percent > 0 ? "Optimal window" : "Charge now");
  animateNumber("expectedCarbon", expectedCarbon, (value) => `${value.toFixed(2)} gCO2/kWh`);
  animateNumber("savingsValue", recommendation.carbon_savings_percent, (value) => `${value.toFixed(2)}%`);
  animateNumber("co2SavedValue", recommendation.co2_saved_kg, (value) => `${value.toFixed(3)} kg`);

  const timelineStart = new Date(grid.timestamp);
  const timelineEnd = forecast.length ? new Date(forecast[forecast.length - 1].timestamp) : new Date(timelineStart.getTime() + 12 * 60 * 60 * 1000);
  const start = new Date(recommendation.start_time);
  const end = new Date(recommendation.end_time);
  const now = new Date();
  const totalSpan = Math.max(timelineEnd - timelineStart, 1);
  const left = Math.max(0, ((start - timelineStart) / totalSpan) * 100);
  const width = Math.max(6, ((end - start) / totalSpan) * 100);
  const nowOffset = Math.min(100, Math.max(0, ((now - timelineStart) / totalSpan) * 100));

  const timelineWindow = getElement("chargingTimelineWindow");
  const timelineMarker = getElement("chargingTimelineMarker");
  const timelineCurrent = getElement("chargingTimelineCurrent");
  if (timelineWindow) {
    timelineWindow.style.left = `${left}%`;
    timelineWindow.style.width = `${Math.min(width, 100 - left)}%`;
  }
  if (timelineMarker) {
    timelineMarker.style.left = `${left}%`;
  }
  if (timelineCurrent) {
    timelineCurrent.style.left = `${nowOffset}%`;
  }
  setText("timelineStartLabel", formatShortTime(timelineStart));
  setText("timelineEndLabel", formatShortTime(timelineEnd));

  animateNumber("chargeNowEmissions", chargeNowKg, (value) => `${value.toFixed(2)} kg`);
  animateNumber("energyNeededValue", recommendation.energy_needed_kwh || 0, (value) => `${value.toFixed(2)} kWh`);
  setText("durationValue", `Estimated charging time: ${durationHours.toFixed(2)} hours.`);
  animateNumber("recommendedSavingsPulse", Math.max(0, chargeNowKg - ((recommendation.energy_needed_kwh || 0) * expectedCarbon) / 1000), (value) => `${value.toFixed(2)} kg saved`);
  setText("chargeNowNarrative", `As per the current grid, charging now will emit about ${chargeNowKg.toFixed(2)} kg CO2.`);

  setText("trendSubtitle", `Recent readings (${history.length}) plus 12-hour forecast with the charging marker.`);
}

function renderError(error) {
  setLoadingState(false);
  setText("updatedAt", "Unavailable");
  setText("statusMessage", `Data unavailable: ${error.message}`);
  setText("generationValue", "Generation: -- MW");
  setText("demandValue", "Demand: -- MW");
  setText("dataStatusBadge", "Unavailable");
  setClassName(
    "dataStatusBadge",
    "inline-flex rounded-full border border-rose-400/30 bg-rose-500/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-rose-200",
  );
  setText("historyBadge", "History unavailable");
  setClassName(
    "historyBadge",
    "inline-flex rounded-full border border-rose-400/20 bg-rose-500/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-rose-100",
  );
  setPillClass("liveIndicator", "offline");
  setPillClass("trendLiveBadge", "offline");
  setHtml("liveIndicator", '<span class="blink-dot"></span>Offline');
  setHtml("trendLiveBadge", '<span class="blink-dot"></span>Telemetry paused');
  setText("generationInsight", "Live source cards will appear when generation data becomes available.");
}

function renderStatus(status) {
  const badge = getElement("dataStatusBadge");
  const historyBadge = getElement("historyBadge");
  const cadence = getElement("refreshCadence");

  if (cadence) {
    cadence.textContent = `${Math.round(status.auto_refresh_seconds / 60)}-minute polling`;
  }
  setText("refreshCadenceBadge", `${Math.round(status.auto_refresh_seconds / 60)}-minute polling`);
  const liveLabel = status.status === "live" ? "Live" : status.status === "cached" ? "Cached" : "Offline";
  const trendLabel = status.status === "live" ? "Live telemetry" : status.status === "cached" ? "Cached telemetry" : "Telemetry paused";
  setPillClass("liveIndicator", status.status === "live" ? "live" : status.status === "cached" ? "cached" : "offline");
  setPillClass("trendLiveBadge", status.status === "live" ? "live" : status.status === "cached" ? "cached" : "offline");
  setHtml("liveIndicator", `<span class="blink-dot"></span>${liveLabel}`);
  setHtml("trendLiveBadge", `<span class="blink-dot"></span>${trendLabel}`);

  if (badge && status.status === "live") {
    badge.textContent = "Live NPP data";
    badge.className =
      "inline-flex rounded-full border border-emerald-400/30 bg-emerald-500/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-emerald-200";
  } else if (badge && status.status === "cached") {
    badge.textContent = "Cached fallback";
    badge.className =
      "inline-flex rounded-full border border-amber-400/30 bg-amber-500/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-amber-200";
  } else if (badge) {
    badge.textContent = "Unavailable";
    badge.className =
      "inline-flex rounded-full border border-rose-400/30 bg-rose-500/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-rose-200";
  }

  const points = status.valid_history_points ?? 0;
  const recommended = status.recommended_history_points ?? 12;
  if (historyBadge && points >= recommended) {
    historyBadge.textContent = `Forecast ready (${points} points)`;
    historyBadge.className =
      "inline-flex rounded-full border border-cyan-400/30 bg-cyan-500/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-cyan-200";
  } else if (historyBadge) {
    historyBadge.textContent = `Building history (${points}/${recommended})`;
    historyBadge.className =
      "inline-flex rounded-full border border-slate-700 bg-slate-900/70 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-slate-300";
  }
}

function updateRangeFill(input) {
  if (!input) {
    return;
  }
  const min = Number(input.min || 0);
  const max = Number(input.max || 100);
  const value = Number(input.value || 0);
  const percent = ((value - min) / (max - min || 1)) * 100;
  input.style.setProperty("--range-fill", `${percent}%`);
}

function normalizeChargeTargets() {
  const current = Number(getElement("currentCharge")?.value || 0);
  const targetInput = getElement("targetCharge");
  const targetSlider = getElement("targetChargeSlider");
  if (!targetInput || !targetSlider) {
    return;
  }
  if (Number(targetInput.value || 0) < current) {
    targetInput.value = String(current);
    targetSlider.value = String(current);
    updateRangeFill(targetSlider);
  }
}

function syncControlPair(inputId, sliderId, options = {}) {
  const input = getElement(inputId);
  const slider = getElement(sliderId);
  if (!input || !slider) {
    return;
  }

  const syncFromInput = () => {
    slider.value = options.allowBlank && input.value === "" ? "0" : input.value;
    updateRangeFill(slider);
    if (options.normalize) {
      options.normalize();
    }
  };

  const syncFromSlider = () => {
    input.value = options.zeroMeansBlank && Number(slider.value) === 0 ? "" : slider.value;
    updateRangeFill(slider);
    if (options.normalize) {
      options.normalize();
    }
  };

  input.addEventListener("input", syncFromInput);
  slider.addEventListener("input", syncFromSlider);
  updateRangeFill(slider);
}

function getChargingParams() {
  normalizeChargeTargets();
  const params = new URLSearchParams({
    battery_capacity: document.getElementById("batteryCapacity").value || "60",
    current_charge: document.getElementById("currentCharge").value || "30",
    target_charge: document.getElementById("targetCharge").value || "80",
    charging_power: document.getElementById("chargingPower").value || "7.2",
    urgency_factor: document.getElementById("urgencyFactor").value || "0.25",
  });

  const deadlineHours = document.getElementById("deadlineHours").value;
  if (deadlineHours && Number(deadlineHours) > 0) {
    params.set("deadline_hours", deadlineHours);
  }

  return params;
}

async function refreshDashboard({ force = false } = {}) {
  if (state.refreshInFlight) {
    state.queuedForceRefresh = state.queuedForceRefresh || force;
    return;
  }

  state.refreshInFlight = true;
  if (refreshButton) {
    refreshButton.disabled = true;
    refreshButton.textContent = "Refreshing...";
  }

  try {
    const chargingParams = getChargingParams();
    const refreshSuffix = force ? "?refresh=true" : "";
    const [grid, carbon, forecast, recommendation, status] = await Promise.all([
      fetchJson(`/grid-data${refreshSuffix}`),
      fetchJson(`/carbon-intensity${refreshSuffix}`),
      fetchJson("/forecast"),
      fetchJson(`/optimal-charging?${chargingParams.toString()}`),
      fetchJson("/system-status"),
    ]);

    renderCurrent(grid, carbon, recommendation, forecast.history, forecast.forecast);
    renderStatus(status);
    renderEnergySourceCards(grid.generation);
    renderGenerationChart(grid.generation);
    renderTrendChart(forecast.history, forecast.forecast, recommendation);

    if (state.lastGridTimestamp && state.lastGridTimestamp !== grid.timestamp) {
      ["heroPanel", "carbonCard", "chargingCard", "generationPanel", "trendPanel", "controlsPanel", "insightPanel"].forEach(flashPanel);
    }
    state.lastGridTimestamp = grid.timestamp;
  } catch (error) {
    renderError(error);
    console.error(error);
  } finally {
    state.refreshInFlight = false;
    if (refreshButton) {
      refreshButton.disabled = false;
      refreshButton.textContent = "Refresh now";
    }
    if (state.queuedForceRefresh) {
      state.queuedForceRefresh = false;
      refreshDashboard({ force: true });
    }
  }
}

refreshDashboard({ force: true });
setInterval(() => refreshDashboard({ force: true }), 60 * 1000);
if (chargingForm) {
  syncControlPair("batteryCapacity", "batteryCapacitySlider");
  syncControlPair("currentCharge", "currentChargeSlider", { normalize: normalizeChargeTargets });
  syncControlPair("targetCharge", "targetChargeSlider", { normalize: normalizeChargeTargets });
  syncControlPair("chargingPower", "chargingPowerSlider");
  syncControlPair("urgencyFactor", "urgencyFactorSlider");
  syncControlPair("deadlineHours", "deadlineHoursSlider", { allowBlank: true, zeroMeansBlank: true });

  chargingForm.addEventListener("input", () => {
    clearTimeout(state.refreshTimer);
    state.refreshTimer = setTimeout(() => refreshDashboard(), 240);
  });
}

if (refreshButton) {
  refreshButton.addEventListener("click", () => refreshDashboard({ force: true }));
}
