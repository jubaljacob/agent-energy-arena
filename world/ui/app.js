(() => {
  const canvas = document.getElementById("grid");
  const ctx = canvas.getContext("2d");
  const buildList = document.getElementById("buildlist");
  const toastEl = document.getElementById("toast");
  const nextDayBtn = document.getElementById("next-day");
  const buildHint = document.getElementById("buildhint");
  const subSurveyBtn = document.getElementById("mode-survey");
  const subDrillBtn = document.getElementById("mode-drill");
  const surveySizeInput = document.getElementById("survey-size");
  const surveyCostEl = document.getElementById("survey-cost-preview");
  const modal = document.getElementById("modal");
  const modalBody = document.getElementById("modal-body");
  const modalCancel = document.getElementById("modal-cancel");
  const modalConfirm = document.getElementById("modal-confirm");
  const subTargetEl = document.getElementById("sub-target");
  const drillTypeRadios = () =>
    document.querySelectorAll('input[name="drill-type"]');

  const els = {
    day: document.getElementById("day"),
    treasury: document.getElementById("treasury"),
    population: document.getElementById("population"),
    happiness: document.getElementById("happiness"),
    balance: document.getElementById("balance"),
  };

  const TILE_COLORS = {
    town_hall: "#d4a72c",
    road: "#6e7177",
    house: "#4ea3ff",
    commercial: "#9d6cff",
    industrial: "#ff7a59",
    park: "#3fbf7f",
    pipeline: "#bdb6a8",
    solar_farm: "#f5d76e",
    wind_turbine: "#6dd5ed",
    coal_plant: "#c97676",
    gas_peaker: "#d09bff",
    refinery: "#e07a4d",
  };

  const PLANT_TYPES = ["solar_farm", "wind_turbine", "coal_plant", "gas_peaker"];

  let cols = 32;
  let rows = 32;
  let tiles = [];
  let wells = [];
  let activeEvents = [];
  let historicalEvents = [];
  let summary = {};
  let treasury = 0;
  let catalog = null;       // map of tile_type -> tile spec (buildable only)
  let catalogRaw = null;    // full /catalog payload (incl. subsurface block)
  let selectedType = null;
  let hoverCell = null;

  // Mode state: "build" (legacy selectedType drives detail), "survey",
  // "drill", or null. Only one mode is active at a time; selecting a build
  // tile, the Survey button, or the Drill button deactivates the others.
  let mode = null;
  let surveySize = 8;
  let drillWellType = "production";
  // Locked drill anchor (picked via voxel-click in the Subsurface tab).
  // null until the user picks a voxel. Survives slice-selector changes.
  let drillAnchor = null;
  // Pending modal action — populated when the dry-hole modal is open.
  let pendingDrill = null;

  const REFINERY_YIELD = 0.85;
  const REFINERY_MAX_BBL_DAY = 500;

  function showToast(msg, kind = "error") {
    toastEl.textContent = msg;
    toastEl.className = `toast show ${kind}`;
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => {
      toastEl.className = "toast";
    }, 1800);
  }

  function cellSize() {
    return { cw: canvas.width / cols, ch: canvas.height / rows };
  }

  function drawGrid() {
    const w = canvas.width;
    const h = canvas.height;
    const { cw, ch } = cellSize();
    ctx.clearRect(0, 0, w, h);

    for (const t of tiles) {
      ctx.fillStyle = TILE_COLORS[t.type] || "#888";
      ctx.fillRect(t.x * cw, t.y * ch, cw, ch);
      if (t.type === "town_hall") {
        ctx.fillStyle = "#1a1c22";
        ctx.font = `${Math.floor(ch * 0.6)}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("⌂", t.x * cw + cw / 2, t.y * ch + ch / 2);
      }
    }

    ctx.strokeStyle = "#2a2d34";
    ctx.lineWidth = 1;
    for (let i = 0; i <= cols; i++) {
      const x = Math.round(i * cw) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h);
      ctx.stroke();
    }
    for (let j = 0; j <= rows; j++) {
      const y = Math.round(j * ch) + 0.5;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }

    if (selectedType && mode === "build" && hoverCell) {
      const valid = isPlacementValid(hoverCell.x, hoverCell.y, selectedType);
      ctx.fillStyle = valid ? "rgba(63,191,127,0.35)" : "rgba(255,80,80,0.35)";
      ctx.fillRect(hoverCell.x * cw, hoverCell.y * ch, cw, ch);
    }

    // Issue 21: explored-columns hatching (visible in all modes).
    const explored = exploredColumnsSet();
    if (explored.size > 0) {
      ctx.save();
      ctx.strokeStyle = "rgba(245,215,110,0.18)";
      ctx.lineWidth = 1;
      for (const key of explored) {
        const [ex, ey] = key.split(",").map(Number);
        if (tileAt(ex, ey)) continue; // tiles already render their own swatch
        // Diagonal hatch: two thin lines per cell.
        const x0 = ex * cw;
        const y0 = ey * ch;
        ctx.beginPath();
        ctx.moveTo(x0, y0 + ch * 0.3);
        ctx.lineTo(x0 + cw * 0.7, y0 + ch);
        ctx.moveTo(x0 + cw * 0.3, y0);
        ctx.lineTo(x0 + cw, y0 + ch * 0.7);
        ctx.stroke();
      }
      ctx.restore();
    }

    // Survey mode: clipped N×N footprint + affordability tint.
    if (mode === "survey" && hoverCell) {
      const [x0, y0, x1, y1] = columnBounds(hoverCell.x, hoverCell.y, surveySize, cols, rows);
      const cost = surveyCost(surveySize) || 0;
      const affordable = treasury >= cost;
      ctx.save();
      ctx.fillStyle = affordable ? "rgba(245,215,110,0.22)" : "rgba(255,80,80,0.28)";
      ctx.fillRect(x0 * cw, y0 * ch, (x1 - x0) * cw, (y1 - y0) * ch);
      ctx.strokeStyle = affordable ? "rgba(245,215,110,0.7)" : "rgba(255,80,80,0.85)";
      ctx.lineWidth = 1.5;
      ctx.strokeRect(x0 * cw, y0 * ch, (x1 - x0) * cw, (y1 - y0) * ch);
      // Resurvey overlay: cells already in explored set get a darker hatch.
      ctx.strokeStyle = "rgba(245,215,110,0.55)";
      ctx.lineWidth = 1;
      for (let yy = y0; yy < y1; yy++) {
        for (let xx = x0; xx < x1; xx++) {
          if (!explored.has(`${xx},${yy}`)) continue;
          const px = xx * cw;
          const py = yy * ch;
          ctx.beginPath();
          ctx.moveTo(px, py);
          ctx.lineTo(px + cw, py + ch);
          ctx.moveTo(px + cw, py);
          ctx.lineTo(px, py + ch);
          ctx.stroke();
        }
      }
      ctx.restore();
    }

    // Drill mode: crosshair on the locked anchor; color encodes guard state.
    if (mode === "drill" && drillAnchor) {
      const occupied = !!tileAt(drillAnchor.x, drillAnchor.y) || !!wellAt(drillAnchor.x, drillAnchor.y);
      const dryHole = !poolHasHc(drillAnchor.x, drillAnchor.y, drillAnchor.target_z);
      let color;
      if (occupied) color = "#ff5050";
      else if (dryHole) color = "#f5d76e";
      else color = "#ff7a59";
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      const cx = drillAnchor.x * cw + cw / 2;
      const cy = drillAnchor.y * ch + ch / 2;
      const r = Math.min(cw, ch) * 0.4;
      ctx.beginPath();
      ctx.moveTo(cx - r, cy);
      ctx.lineTo(cx + r, cy);
      ctx.moveTo(cx, cy - r);
      ctx.lineTo(cx, cy + r);
      ctx.stroke();
      ctx.strokeRect(drillAnchor.x * cw + 0.5, drillAnchor.y * ch + 0.5, cw - 1, ch - 1);
      ctx.restore();
    }
  }

  function tileAt(x, y) {
    return tiles.find((t) => t.x === x && t.y === y) || null;
  }

  function wellAt(x, y) {
    return wells.find((w) => w.x === x && w.y === y) || null;
  }

  function roadNetwork() {
    const start = tiles.find((t) => t.type === "town_hall");
    if (!start) return new Set();
    const isRoad = new Map();
    for (const t of tiles) {
      if (t.type === "road" || t.type === "town_hall") {
        isRoad.set(`${t.x},${t.y}`, t);
      }
    }
    const seen = new Set([`${start.x},${start.y}`]);
    const stack = [[start.x, start.y]];
    while (stack.length) {
      const [x, y] = stack.pop();
      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        const nx = x + dx;
        const ny = y + dy;
        const key = `${nx},${ny}`;
        if (seen.has(key)) continue;
        if (!isRoad.has(key)) continue;
        seen.add(key);
        stack.push([nx, ny]);
      }
    }
    return seen;
  }

  function isPlacementValid(x, y, tileType) {
    if (!catalog) return false;
    if (x < 0 || y < 0 || x >= cols || y >= rows) return false;
    if (tileAt(x, y)) return false;
    const spec = catalog[tileType];
    if (!spec) return false;
    if (treasury < spec.capex) return false;
    if (spec.requires_road) {
      const net = roadNetwork();
      const adj = [[x + 1, y], [x - 1, y], [x, y + 1], [x, y - 1]];
      if (!adj.some(([ax, ay]) => net.has(`${ax},${ay}`))) return false;
    }
    return true;
  }

  async function loadCatalog() {
    try {
      const res = await fetch("/catalog");
      const data = await res.json();
      catalogRaw = data;
      catalog = {};
      for (const entry of data.tiles) {
        if (entry.buildable) catalog[entry.tile_type] = entry;
      }
      renderBuildMenu();
      updateSurveyCostPreview();
    } catch (err) {
      console.error("catalog load failed", err);
    }
  }

  // Mirrors world.subsurface._column_bounds(x, y, size, world_w, world_h).
  // Returns inclusive-exclusive [x0, x1) × [y0, y1) range clipped to grid.
  function columnBounds(x, y, size, w, h) {
    const half = Math.floor(size / 2);
    return [
      Math.max(0, x - half),
      Math.max(0, y - half),
      Math.min(w, x - half + size),
      Math.min(h, y - half + size),
    ];
  }

  // base_cost * (size / base_size)**2 — matches world.subsurface.survey_cost.
  // Returns null until /catalog has loaded.
  function surveyCost(size) {
    if (!catalogRaw || !catalogRaw.subsurface) return null;
    const s = catalogRaw.subsurface.survey;
    return s.base_cost * Math.pow(size / s.base_size, 2);
  }

  function drillCapex(wellType) {
    if (!catalogRaw || !catalogRaw.subsurface) return 0;
    return catalogRaw.subsurface.drill[wellType].capex;
  }

  function clampSurveySize(raw) {
    let n = parseInt(raw, 10);
    if (isNaN(n)) return surveySize;
    const s = catalogRaw && catalogRaw.subsurface ? catalogRaw.subsurface.survey : { min_size: 4, max_size: 16 };
    if (n < s.min_size) n = s.min_size;
    if (n > s.max_size) n = s.max_size;
    return n;
  }

  function updateSurveyCostPreview() {
    if (!surveyCostEl) return;
    const c = surveyCost(surveySize);
    surveyCostEl.textContent = c == null ? "—" : `$${Math.round(c).toLocaleString()}`;
  }

  // Set of "x,y" keys for columns that have at least one revealed voxel.
  // Mirrors `subsurface.explored_columns` for HC-bearing columns; truly
  // empty surveyed columns are not in /reservoirs so the overlay won't mark
  // them — acceptable for a hover hint (server is the source of truth).
  function exploredColumnsSet() {
    const s = new Set();
    for (const v of revealedVoxels) s.add(`${v.x},${v.y}`);
    return s;
  }

  // Dry-hole guard: returns true iff no known HC voxel sits within ±1 of
  // (x, y, target_z). Uses the /reservoirs?top_k=4096 payload that the
  // Subsurface tab already polls.
  function poolHasHc(x, y, z) {
    for (const v of revealedVoxels) {
      if (Math.abs(v.x - x) <= 1 && Math.abs(v.y - y) <= 1 && Math.abs(v.z - z) <= 1) {
        if (v.oil_estimate_bbl > 0) return true;
      }
    }
    return false;
  }

  function setMode(next) {
    mode = next;
    if (next !== "build") {
      selectedType = null;
      for (const node of buildList.children) node.classList.remove("selected");
    }
    if (next !== "drill") {
      drillAnchor = null;
      renderSubsurface();
    }
    subSurveyBtn.classList.toggle("selected", next === "survey");
    subDrillBtn.classList.toggle("selected", next === "drill");
    canvas.classList.toggle("crosshair", next === "survey" || next === "drill");
    refreshBuildHint();
    drawGrid();
  }

  function refreshBuildHint() {
    if (!buildHint) return;
    if (mode === "survey") {
      buildHint.textContent =
        "Survey mode — click canvas to anchor. Right-click or Esc to cancel.";
    } else if (mode === "drill") {
      const target = drillAnchor
        ? ` Locked at (${drillAnchor.x}, ${drillAnchor.y}, z=${drillAnchor.target_z}).`
        : "";
      buildHint.textContent =
        `Drill mode — pick a voxel in the Subsurface tab, then click the canvas.${target} Right-click or Esc to cancel.`;
    } else if (mode === "build" && selectedType) {
      buildHint.textContent = `Build mode — click a grid cell to place ${selectedType}. Right-click to demolish.`;
    } else {
      buildHint.textContent = "Click a tile type, then click the grid. Right-click a tile to demolish.";
    }
  }

  function renderBuildMenu() {
    if (!catalog) return;
    buildList.innerHTML = "";
    const order = [
      "road",
      "house",
      "commercial",
      "industrial",
      "park",
      "pipeline",
      "refinery",
      "solar_farm",
      "wind_turbine",
      "gas_peaker",
      "coal_plant",
    ];
    for (const tt of order) {
      const spec = catalog[tt];
      if (!spec) continue;
      const li = document.createElement("li");
      li.dataset.type = tt;
      li.className = "buildItem";
      li.innerHTML = `
        <span class="swatch" style="background:${TILE_COLORS[tt] || "#888"}"></span>
        <div class="bi-text">
          <div class="bi-name">${tt}</div>
          <div class="bi-desc">${spec.description}</div>
          <div class="bi-cost">$${spec.capex.toLocaleString()} · $${spec.opex_per_day}/day</div>
        </div>
      `;
      li.addEventListener("click", () => {
        const turningOff = selectedType === tt;
        selectedType = turningOff ? null : tt;
        for (const node of buildList.children) node.classList.remove("selected");
        if (selectedType) li.classList.add("selected");
        setMode(selectedType ? "build" : null);
      });
      buildList.appendChild(li);
    }
  }

  function gridCellFromEvent(ev) {
    const rect = canvas.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const py = ev.clientY - rect.top;
    return {
      x: Math.floor((px / rect.width) * cols),
      y: Math.floor((py / rect.height) * rows),
    };
  }

  // Subsurface tool palette wiring.
  if (subSurveyBtn) {
    subSurveyBtn.addEventListener("click", (ev) => {
      // Clicking the size input should not toggle the mode.
      if (ev.target.tagName === "INPUT") return;
      setMode(mode === "survey" ? null : "survey");
    });
  }
  if (subDrillBtn) {
    subDrillBtn.addEventListener("click", (ev) => {
      if (ev.target.tagName === "INPUT" || ev.target.tagName === "LABEL") return;
      setMode(mode === "drill" ? null : "drill");
    });
  }
  if (surveySizeInput) {
    surveySizeInput.addEventListener("input", () => {
      surveySize = clampSurveySize(surveySizeInput.value);
      // Don't snap the input while typing; only refresh the preview text.
      updateSurveyCostPreview();
      if (mode === "survey") drawGrid();
    });
    surveySizeInput.addEventListener("blur", () => {
      surveySize = clampSurveySize(surveySizeInput.value);
      surveySizeInput.value = String(surveySize);
      updateSurveyCostPreview();
      drawGrid();
    });
  }
  for (const radio of drillTypeRadios()) {
    radio.addEventListener("change", () => {
      drillWellType = radio.value;
      drawGrid();
    });
  }

  // Modal wiring.
  function hideModal() {
    if (modal) modal.classList.add("hidden");
    pendingDrill = null;
  }
  function showModal(bodyText, onConfirm) {
    if (!modal) return;
    modalBody.textContent = bodyText;
    modal.classList.remove("hidden");
    pendingDrill = onConfirm;
  }
  if (modalCancel) modalCancel.addEventListener("click", () => hideModal());
  if (modalConfirm) {
    modalConfirm.addEventListener("click", async () => {
      const fn = pendingDrill;
      hideModal();
      if (fn) await fn();
    });
  }

  window.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    const tag = (ev.target && ev.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if (modal && !modal.classList.contains("hidden")) {
      hideModal();
      return;
    }
    if (mode) setMode(null);
  });

  canvas.addEventListener("mousemove", (ev) => {
    const cell = gridCellFromEvent(ev);
    if (!hoverCell || hoverCell.x !== cell.x || hoverCell.y !== cell.y) {
      hoverCell = cell;
      drawGrid();
    }
    updateHoverTooltip();
  });
  canvas.addEventListener("mouseleave", () => {
    hoverCell = null;
    canvas.title = "";
    drawGrid();
  });

  function updateHoverTooltip() {
    if (!hoverCell) {
      canvas.title = "";
      return;
    }
    if (mode === "survey") {
      const [x0, y0, x1, y1] = columnBounds(hoverCell.x, hoverCell.y, surveySize, cols, rows);
      const cells = (x1 - x0) * (y1 - y0);
      const cost = surveyCost(surveySize) || 0;
      const explored = exploredColumnsSet();
      let prior = 0;
      for (let yy = y0; yy < y1; yy++) {
        for (let xx = x0; xx < x1; xx++) {
          if (explored.has(`${xx},${yy}`)) prior += 1;
        }
      }
      const label = prior > 0 ? "resurvey" : `survey @ (${hoverCell.x}, ${hoverCell.y}) size=${surveySize}`;
      const priorNote = prior > 0 ? ` (${prior} previously surveyed)` : "";
      canvas.title = `${label} · cost $${Math.round(cost).toLocaleString()} · ${cells} cells${priorNote}`;
    } else if (mode === "drill") {
      if (drillAnchor) {
        canvas.title = `drill @ (${drillAnchor.x}, ${drillAnchor.y}) target_z=${drillAnchor.target_z} type=${drillWellType}`;
      } else {
        canvas.title = "Pick a voxel in the Subsurface tab first to lock a drill target.";
      }
    } else {
      canvas.title = "";
    }
  }

  canvas.addEventListener("click", async (ev) => {
    const { x, y } = gridCellFromEvent(ev);
    if (mode === "survey") {
      await handleSurveyClick(x, y);
      return;
    }
    if (mode === "drill") {
      await handleDrillClick(x, y);
      return;
    }
    if (!selectedType) return;
    try {
      const res = await fetch("/build", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ tile_type: selectedType, x, y }),
      });
      const body = await res.json();
      if (body.ok) showToast(`built ${selectedType} at (${x}, ${y})`, "ok");
      else showToast(`build rejected: ${body.error}`, "error");
      tick();
    } catch (err) {
      showToast(`network error: ${err}`, "error");
    }
  });

  canvas.addEventListener("contextmenu", async (ev) => {
    ev.preventDefault();
    // While a subsurface mode is active, right-click cancels the mode and
    // does NOT fall through to /demolish.
    if (mode === "survey" || mode === "drill") {
      setMode(null);
      return;
    }
    const { x, y } = gridCellFromEvent(ev);
    try {
      const res = await fetch("/demolish", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ x, y }),
      });
      const body = await res.json();
      if (body.ok) showToast(`demolished at (${x}, ${y})`, "ok");
      else showToast(`demolish rejected: ${body.error}`, "error");
      tick();
    } catch (err) {
      showToast(`network error: ${err}`, "error");
    }
  });

  async function handleSurveyClick(x, y) {
    const cost = surveyCost(surveySize) || 0;
    if (treasury < cost) {
      showToast(`survey rejected: insufficient_funds`, "error");
      return;
    }
    try {
      const res = await fetch("/survey", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ x, y, size: surveySize }),
      });
      const body = await res.json();
      if (body.ok) {
        const voxList = (body.result && body.result.voxels) || [];
        const nHc = voxList.filter((v) => (v.oil_estimate_bbl || 0) > 0).length;
        showToast(`survey done — ${nHc} HC voxels revealed · click to open Subsurface tab`, "ok-bridge");
        // Toast click opens Subsurface tab + jumps to slice=anchor_y.
        toastEl.onclick = () => {
          activateSubsurfaceTab();
          if (subAxisEl) subAxisEl.value = "y";
          if (subSliceEl) {
            subSliceEl.value = String(y);
            renderSubsurface();
          }
          toastEl.className = "toast";
          toastEl.onclick = null;
        };
      } else {
        showToast(`survey rejected: ${body.error}`, "error");
      }
      await refreshRevealed();
      tick();
    } catch (err) {
      showToast(`network error: ${err}`, "error");
    }
  }

  async function handleDrillClick(_surfaceX, _surfaceY) {
    if (!drillAnchor) {
      showToast("pick a voxel in the Subsurface tab first", "error");
      return;
    }
    const { x: ax, y: ay, target_z: az } = drillAnchor;
    if (tileAt(ax, ay) || wellAt(ax, ay)) {
      showToast("drill rejected: occupied", "error");
      return;
    }
    const fire = () => fireDrill(ax, ay);
    if (!poolHasHc(ax, ay, az)) {
      const capex = drillCapex(drillWellType);
      showModal(
        `No surveyed HC voxels in the 3×3×3 drainage pool around (${ax}, ${ay}, ${az}). The well may produce 0 bbl/day — $${capex.toLocaleString()} CAPEX at risk.`,
        fire,
      );
      return;
    }
    await fire();
  }

  async function fireDrill(surfaceX, surfaceY) {
    try {
      const res = await fetch("/drill", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          x: surfaceX,
          y: surfaceY,
          target_z: drillAnchor.target_z,
          well_type: drillWellType,
        }),
      });
      const body = await res.json();
      if (body.ok) {
        const wellId = body.result && (body.result.id || body.result.well_id) || "";
        showToast(
          `drilled ${drillWellType} well at (${surfaceX}, ${surfaceY}, ${drillAnchor.target_z})${wellId ? ` — id=${wellId}` : ""}`,
          "ok",
        );
        drillAnchor = null;
        renderSubsurface();
        refreshBuildHint();
      } else {
        showToast(`drill rejected: ${body.error}`, "error");
      }
      tick();
    } catch (err) {
      showToast(`network error: ${err}`, "error");
    }
  }

  function activateSubsurfaceTab() {
    for (const btn of tabButtons) btn.classList.toggle("active", btn.dataset.tab === "subsurface");
    for (const p of tabPanels) p.classList.toggle("active", p.id === "tab-subsurface");
    refreshRevealed().then(renderSubsurface);
  }

  nextDayBtn.addEventListener("click", async () => {
    nextDayBtn.disabled = true;
    try {
      await fetch("/step", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ days: 1 }),
      });
      tick();
    } finally {
      nextDayBtn.disabled = false;
    }
  });

  // Tab switching ---------------------------------------------------------
  const tabButtons = document.querySelectorAll(".tab");
  const tabPanels = document.querySelectorAll(".tabpanel");
  for (const btn of tabButtons) {
    btn.addEventListener("click", async () => {
      const target = btn.dataset.tab;
      for (const b of tabButtons) b.classList.toggle("active", b === btn);
      for (const p of tabPanels) p.classList.toggle("active", p.id === `tab-${target}`);
      if (target === "subsurface") {
        await refreshRevealed();
        renderSubsurface();
      }
    });
  }

  // Power tab rendering ---------------------------------------------------
  const chartEl = document.getElementById("powerchart");
  const plantListEl = document.getElementById("plantlist");

  function renderPowerChart(supply, demand) {
    if (!chartEl) return;
    chartEl.innerHTML = "";
    if (!supply || !demand || supply.length === 0) {
      const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
      t.setAttribute("x", "50%");
      t.setAttribute("y", "50%");
      t.setAttribute("fill", "#5a5d65");
      t.setAttribute("text-anchor", "middle");
      t.setAttribute("font-size", "12");
      t.textContent = "no data — step a day to see hourly trace";
      chartEl.appendChild(t);
      return;
    }
    const W = 480;
    const H = 200;
    const padX = 28;
    const padY = 8;
    const maxY = Math.max(...supply, ...demand, 1) * 1.1;
    const path = (series, color) => {
      const pts = series.map((v, i) => {
        const x = padX + (i / (series.length - 1)) * (W - padX * 2);
        const y = H - padY - (v / maxY) * (H - padY * 2);
        return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
      });
      const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
      p.setAttribute("d", pts.join(" "));
      p.setAttribute("stroke", color);
      p.setAttribute("stroke-width", "2");
      p.setAttribute("fill", "none");
      chartEl.appendChild(p);
    };
    // Y-axis label (max).
    const lbl = document.createElementNS("http://www.w3.org/2000/svg", "text");
    lbl.setAttribute("x", "4");
    lbl.setAttribute("y", "14");
    lbl.setAttribute("fill", "#5a5d65");
    lbl.setAttribute("font-size", "10");
    lbl.textContent = `${Math.round(maxY)} kW`;
    chartEl.appendChild(lbl);
    path(demand, "#ff7a59");
    path(supply, "#4ea3ff");
  }

  function renderPlantList(allTiles) {
    if (!plantListEl) return;
    plantListEl.innerHTML = "";
    const plants = allTiles.filter((t) => PLANT_TYPES.includes(t.type));
    if (plants.length === 0) {
      const li = document.createElement("li");
      li.style.color = "#5a5d65";
      li.style.fontSize = "0.8rem";
      li.textContent = "no plants built";
      plantListEl.appendChild(li);
      return;
    }
    const caps = (catalog && Object.fromEntries(Object.entries(catalog).map(([k, v]) => [k, v.capacity_kw || 1]))) || {};
    for (const p of plants) {
      const cap = caps[p.type] || 1;
      const out = p.current_output_kw || 0;
      const pct = Math.min(100, (out / cap) * 100);
      const li = document.createElement("li");
      li.className = `plantrow ${p.type}`;
      li.innerHTML = `
        <span class="pl-name">${p.type} (${p.x},${p.y})</span>
        <div class="pl-bar"><div class="pl-bar-fill" style="width:${pct.toFixed(1)}%"></div></div>
        <span class="pl-val">${Math.round(out)}/${cap} kW</span>
      `;
      plantListEl.appendChild(li);
    }
  }

  // Subsurface tab rendering -------------------------------------------
  const subAxisEl = document.getElementById("sub-axis");
  const subSliceEl = document.getElementById("sub-slice");
  const subChartEl = document.getElementById("subchart");
  const subStatsEl = document.getElementById("sub-stats");

  let worldDims = { w: 32, h: 32, d: 16 };
  let revealedVoxels = [];

  function maxOilForColorScale() {
    let m = 0;
    for (const v of revealedVoxels) {
      if (v.oil_estimate_bbl > m) m = v.oil_estimate_bbl;
    }
    return m || 1;
  }

  function oilColor(value, maxValue) {
    const t = Math.min(1, Math.max(0, value / maxValue));
    // Cool → warm gradient: dark teal (#2a3a4d) → yellow (#f5d76e).
    const r = Math.round(0x2a + t * (0xf5 - 0x2a));
    const g = Math.round(0x3a + t * (0xd7 - 0x3a));
    const b = Math.round(0x4d + t * (0x6e - 0x4d));
    return `rgb(${r},${g},${b})`;
  }

  async function refreshRevealed() {
    try {
      const res = await fetch("/reservoirs?min_oil=0&top_k=4096");
      if (!res.ok) return;
      const body = await res.json();
      revealedVoxels = body.voxels || [];
    } catch (err) {
      // best-effort; subsurface tab keeps prior data
    }
  }

  function renderSubsurface() {
    if (!subChartEl) return;
    subChartEl.innerHTML = "";
    const axis = subAxisEl.value || "y";
    let slice = parseInt(subSliceEl.value, 10);
    if (isNaN(slice)) slice = 16;
    const W = 640;
    const H = 320;
    // Cross-section dims: (lateral × depth)
    const lateralN = axis === "y" ? worldDims.w : worldDims.h;
    const depthN = worldDims.d;
    const cw = W / lateralN;
    const ch = H / depthN;

    // Outline grid for unrevealed voxels.
    const gridG = document.createElementNS("http://www.w3.org/2000/svg", "g");
    gridG.setAttribute("stroke", "#2f323a");
    gridG.setAttribute("fill", "transparent");
    gridG.setAttribute("stroke-width", "0.5");
    for (let i = 0; i < lateralN; i++) {
      for (let z = 0; z < depthN; z++) {
        const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        r.setAttribute("x", (i * cw).toFixed(2));
        r.setAttribute("y", (z * ch).toFixed(2));
        r.setAttribute("width", cw.toFixed(2));
        r.setAttribute("height", ch.toFixed(2));
        gridG.appendChild(r);
      }
    }
    subChartEl.appendChild(gridG);

    const maxOil = maxOilForColorScale();
    const onSlice = revealedVoxels.filter((v) =>
      axis === "y" ? v.y === slice : v.x === slice
    );
    const filledG = document.createElementNS("http://www.w3.org/2000/svg", "g");
    for (const v of onSlice) {
      const lateral = axis === "y" ? v.x : v.y;
      const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      r.setAttribute("x", (lateral * cw).toFixed(2));
      r.setAttribute("y", (v.z * ch).toFixed(2));
      r.setAttribute("width", cw.toFixed(2));
      r.setAttribute("height", ch.toFixed(2));
      r.setAttribute("fill", oilColor(v.oil_estimate_bbl, maxOil));
      r.setAttribute("stroke", "#1a1c22");
      r.setAttribute("stroke-width", "0.5");
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `(${v.x}, ${v.y}, ${v.z}) — ${Math.round(v.oil_estimate_bbl).toLocaleString()} bbl, ${Math.round(v.perm_estimate_md)} mD`;
      r.appendChild(title);
      if (mode === "drill") {
        r.classList.add("drill-pickable");
        r.addEventListener("click", () => {
          drillAnchor = { x: v.x, y: v.y, target_z: v.z };
          if (subTargetEl) {
            subTargetEl.classList.remove("hidden");
            subTargetEl.textContent = `selected target: (${v.x}, ${v.y}, ${v.z}) — click surface on the Map tab to drill`;
          }
          refreshBuildHint();
          drawGrid();
          renderSubsurface();
        });
      }
      if (
        drillAnchor
        && drillAnchor.target_z === v.z
        && drillAnchor.x === v.x
        && drillAnchor.y === v.y
      ) {
        r.classList.add("drill-picked");
      }
      filledG.appendChild(r);
    }
    subChartEl.appendChild(filledG);

    // Wellhead markers ▼/▲ for wells on this slice.
    const wellG = document.createElementNS("http://www.w3.org/2000/svg", "g");
    for (const w of wells) {
      const offAxis = axis === "y" ? w.y : w.x;
      if (offAxis !== slice) continue;
      const lateral = axis === "y" ? w.x : w.y;
      const symbol = w.type === "production" ? "▼" : "▲";
      const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
      t.setAttribute("x", (lateral * cw + cw / 2).toFixed(2));
      t.setAttribute("y", (w.target_z * ch + ch * 0.78).toFixed(2));
      t.setAttribute("fill", w.type === "production" ? "#3fbf7f" : "#a8d8ff");
      t.setAttribute("font-size", Math.max(8, Math.floor(ch * 0.7)).toString());
      t.setAttribute("text-anchor", "middle");
      t.setAttribute("pointer-events", "none");
      t.textContent = symbol;
      const setpoint = w.setpoint_rate_bbl_day || 0;
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `${w.id} · ${w.type} · (${w.x}, ${w.y}, ${w.target_z}) · setpoint ${Math.round(setpoint)} bbl/d`;
      t.appendChild(title);
      wellG.appendChild(t);
    }
    subChartEl.appendChild(wellG);

    // Axes labels.
    const axisLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
    axisLabel.setAttribute("x", "4");
    axisLabel.setAttribute("y", "14");
    axisLabel.setAttribute("fill", "#8b8f9a");
    axisLabel.setAttribute("font-size", "11");
    axisLabel.textContent = `axis=${axis}, slice=${slice}, depth↓ ${axis === "y" ? "x→" : "y→"}`;
    subChartEl.appendChild(axisLabel);

    subStatsEl.textContent = `${revealedVoxels.length} revealed voxels · max est. ${Math.round(maxOil).toLocaleString()} bbl · ${onSlice.length} on this slice`;

    if (subTargetEl) {
      if (drillAnchor) {
        subTargetEl.classList.remove("hidden");
        subTargetEl.textContent = `selected target: (${drillAnchor.x}, ${drillAnchor.y}, ${drillAnchor.target_z}) — click surface on the Map tab to drill`;
      } else {
        subTargetEl.classList.add("hidden");
        subTargetEl.textContent = "";
      }
    }
  }

  if (subAxisEl) subAxisEl.addEventListener("change", renderSubsurface);
  if (subSliceEl) subSliceEl.addEventListener("input", renderSubsurface);

  // Wells tab rendering ----------------------------------------------------
  const wellsTableBody = document.getElementById("wellstable-body");
  const wellsStatsEl = document.getElementById("wells-stats");
  const refineriesTableBody = document.getElementById("refineriestable-body");
  const refineriesStatsEl = document.getElementById("refineries-stats");
  const financeListEl = document.getElementById("financelist");

  async function setRefineryRate(refineryId, rate) {
    try {
      await fetch("/control/refinery", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ refinery_id: refineryId, rate_bbl_day: rate }),
      });
      tick();
    } catch (err) {
      showToast(`network error: ${err}`, "error");
    }
  }

  function renderRefineries() {
    if (!refineriesTableBody) return;
    refineriesTableBody.innerHTML = "";
    const refineries = tiles.filter((t) => t.type === "refinery");
    if (refineries.length === 0) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.style.color = "#5a5d65";
      td.style.fontSize = "0.8rem";
      td.style.textAlign = "center";
      td.textContent = "no refineries built — POST /build { tile_type: 'refinery' }";
      tr.appendChild(td);
      refineriesTableBody.appendChild(tr);
    } else {
      for (const r of refineries) {
        const setpoint = r.setpoint_rate_bbl_day || 0;
        const throughput = r.current_throughput_bbl_day || 0;
        const refined = throughput * REFINERY_YIELD;
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${r.id}</td>
          <td>(${r.x}, ${r.y})</td>
          <td>
            <input type="range" min="0" max="${REFINERY_MAX_BBL_DAY}" step="10" value="${setpoint}" data-id="${r.id}" />
            <span class="setpoint-val">${Math.round(setpoint)}</span>
          </td>
          <td class="actual">${throughput.toFixed(1)}</td>
          <td class="cumulative">${refined.toFixed(1)}</td>
        `;
        refineriesTableBody.appendChild(tr);
        const slider = tr.querySelector("input[type=range]");
        const valEl = tr.querySelector(".setpoint-val");
        slider.addEventListener("input", () => {
          valEl.textContent = String(slider.value);
        });
        slider.addEventListener("change", () => {
          setRefineryRate(r.id, parseFloat(slider.value));
        });
      }
    }
    if (refineriesStatsEl) {
      const totalSetpoint = refineries.reduce((a, r) => a + (r.setpoint_rate_bbl_day || 0), 0);
      const totalThroughput = refineries.reduce((a, r) => a + (r.current_throughput_bbl_day || 0), 0);
      refineriesStatsEl.textContent = `${refineries.length} refineries · ${totalSetpoint.toFixed(0)} bbl/d setpoint · ${totalThroughput.toFixed(1)} bbl/d throughput`;
    }
  }

  function renderFinance() {
    if (!financeListEl) return;
    financeListEl.innerHTML = "";
    const rows = [
      ["Tax revenue", summary.tax_revenue || 0, "+"],
      ["Power revenue", summary.power_revenue || 0, "+"],
      ["Crude (direct sale)", summary.crude_revenue || 0, "+"],
      ["Refined oil", summary.refined_revenue || 0, "+"],
      ["OPEX", -(summary.opex || 0), "-"],
      ["Fuel cost", -(summary.fuel_cost || 0), "-"],
      ["Carbon cost", -(summary.carbon_cost || 0), "-"],
      ["Blackout penalty", -(summary.blackout_penalty || 0), "-"],
    ];
    for (const [label, value, sign] of rows) {
      const li = document.createElement("li");
      const cls = value > 0 ? "positive" : value < 0 ? "negative" : "neutral";
      li.className = `finance-row ${cls}`;
      li.innerHTML = `<span class="finance-label">${label}</span><span class="finance-value">${sign === "-" ? "-" : ""}$${Math.abs(Math.round(value)).toLocaleString()}</span>`;
      financeListEl.appendChild(li);
    }
  }

  async function setWellRate(wellId, rate) {
    try {
      await fetch("/control/well", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ well_id: wellId, rate_bbl_day: rate }),
      });
      tick();
    } catch (err) {
      showToast(`network error: ${err}`, "error");
    }
  }

  function renderWells() {
    if (!wellsTableBody) return;
    wellsTableBody.innerHTML = "";
    if (wells.length === 0) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 6;
      td.style.color = "#5a5d65";
      td.style.fontSize = "0.8rem";
      td.style.textAlign = "center";
      td.textContent = "no wells drilled — POST /drill { x, y, target_z, well_type }";
      tr.appendChild(td);
      wellsTableBody.appendChild(tr);
    } else {
      for (const w of wells) {
        const tr = document.createElement("tr");
        const setpoint = w.setpoint_rate_bbl_day || 0;
        const cum = w.type === "injection"
          ? (w.cumulative_injected_bbl || 0)
          : (w.cumulative_produced_bbl || 0);
        tr.innerHTML = `
          <td>${w.id}</td>
          <td>${w.type}</td>
          <td>(${w.x}, ${w.y}, ${w.target_z})</td>
          <td>
            <input type="range" min="0" max="200" step="5" value="${setpoint}" data-id="${w.id}" />
            <span class="setpoint-val">${Math.round(setpoint)}</span>
          </td>
          <td class="actual">${(w.current_rate_bbl_day || 0).toFixed(1)}</td>
          <td class="cumulative">${Math.round(cum).toLocaleString()}</td>
        `;
        wellsTableBody.appendChild(tr);
        const slider = tr.querySelector("input[type=range]");
        const valEl = tr.querySelector(".setpoint-val");
        slider.addEventListener("input", () => {
          valEl.textContent = String(slider.value);
        });
        slider.addEventListener("change", () => {
          setWellRate(w.id, parseFloat(slider.value));
        });
      }
    }
    if (wellsStatsEl) {
      const prodWells = wells.filter((w) => w.type === "production");
      const injWells = wells.filter((w) => w.type === "injection");
      const totalProd = prodWells.reduce((a, w) => a + (w.cumulative_produced_bbl || 0), 0);
      const totalInj = injWells.reduce((a, w) => a + (w.cumulative_injected_bbl || 0), 0);
      const prodRate = prodWells.reduce((a, w) => a + (w.current_rate_bbl_day || 0), 0);
      const injRate = injWells.reduce((a, w) => a + (w.current_rate_bbl_day || 0), 0);
      wellsStatsEl.textContent = `${prodWells.length} prod · ${prodRate.toFixed(1)} bbl/d · ${Math.round(totalProd).toLocaleString()} cum bbl  ·  ${injWells.length} inj · ${injRate.toFixed(1)} bbl/d · ${Math.round(totalInj).toLocaleString()} cum bbl`;
    }
  }

  // Events tab rendering ----------------------------------------------------
  const eventsTableBody = document.getElementById("eventstable-body");
  const eventsHistoryBody = document.getElementById("eventshistorytable-body");

  function eventDetail(e) {
    if (e.type === "plant_failure") return `plant_id=${e.plant_id || "?"}`;
    if (e.type === "regulatory_tightening") {
      const occ = e.occurrences_after != null ? ` (#${e.occurrences_after})` : "";
      return `carbon_price → $${(e.severity || 0).toFixed(2)}${occ}`;
    }
    return `×${(e.severity || 1).toFixed(2)}`;
  }

  function renderEvents(today) {
    if (!eventsTableBody) return;
    eventsTableBody.innerHTML = "";
    if (activeEvents.length === 0) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.style.color = "#5a5d65";
      td.style.fontSize = "0.8rem";
      td.style.textAlign = "center";
      td.textContent = "no active events";
      tr.appendChild(td);
      eventsTableBody.appendChild(tr);
    } else {
      for (const e of activeEvents) {
        const tr = document.createElement("tr");
        const ends = e.ends_day == null ? "—" : e.ends_day;
        const left = e.ends_day == null ? "permanent" : Math.max(0, e.ends_day - today);
        tr.innerHTML = `
          <td>${e.type}</td>
          <td>${e.started_day}</td>
          <td>${ends}</td>
          <td>${left}</td>
          <td>${eventDetail(e)}</td>
        `;
        eventsTableBody.appendChild(tr);
      }
    }
    if (eventsHistoryBody) {
      eventsHistoryBody.innerHTML = "";
      const recent = historicalEvents.slice(-50).reverse();
      if (recent.length === 0) {
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.colSpan = 4;
        td.style.color = "#5a5d65";
        td.style.fontSize = "0.8rem";
        td.style.textAlign = "center";
        td.textContent = "no past events";
        tr.appendChild(td);
        eventsHistoryBody.appendChild(tr);
      } else {
        for (const e of recent) {
          const tr = document.createElement("tr");
          const ends = e.ends_day == null ? "—" : e.ends_day;
          tr.innerHTML = `
            <td>${e.type}</td>
            <td>${e.started_day}</td>
            <td>${ends}</td>
            <td>${eventDetail(e)}</td>
          `;
          eventsHistoryBody.appendChild(tr);
        }
      }
    }
  }

  async function tick() {
    try {
      const res = await fetch("/state");
      if (!res.ok) return;
      const s = await res.json();
      cols = s.config.world_w;
      rows = s.config.world_h;
      worldDims = { w: s.config.world_w, h: s.config.world_h, d: s.config.world_d };
      if (subSliceEl && !subSliceEl.dataset.bounded) {
        subSliceEl.max = String(Math.max(0, worldDims.w - 1));
        subSliceEl.dataset.bounded = "1";
      }
      tiles = s.tiles || [];
      wells = s.wells || [];
      activeEvents = s.active_events || [];
      historicalEvents = s.historical_events || [];
      summary = s.today_summary_so_far || {};
      treasury = s.treasury;
      els.day.textContent = s.day;
      els.treasury.textContent = Math.round(s.treasury).toLocaleString();
      els.population.textContent = s.population;
      els.happiness.textContent = s.happiness.toFixed(2);
      const balanceState = (s.power_now && s.power_now.balance_state) || "—";
      els.balance.textContent = balanceState;
      els.balance.className = `balance-badge ${balanceState}`;
      renderPowerChart(s.last_day_supply_kw_by_hour, s.last_day_demand_kw_by_hour);
      renderPlantList(tiles);
      renderWells();
      renderRefineries();
      renderFinance();
      renderEvents(s.day);
      drawGrid();
      // Refresh revealed voxels lazily when the subsurface tab is visible.
      const subPanel = document.getElementById("tab-subsurface");
      if (subPanel && subPanel.classList.contains("active")) {
        await refreshRevealed();
        renderSubsurface();
      }
    } catch (err) {
      // Server may not be up yet during boot — keep polling.
    }
  }

  loadCatalog();
  drawGrid();
  tick();
  setInterval(tick, 500);
})();
