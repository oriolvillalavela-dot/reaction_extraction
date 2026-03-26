(() => {
  // Elements
  const tabs = document.getElementById("tabs");
  const inputArea = document.getElementById("inputArea");
  const runBtn = document.getElementById("runBtn");
  const progressBar = document.getElementById("progressBar");
  const stats = document.getElementById("stats");
  const fullToggle = document.getElementById("fullToggle");
  const wideToggle = document.getElementById("wideToggle");
  const resultsBody = document.getElementById("resultsBody");
  const table = document.getElementById("resultsTable");
  const copyInchiBtn = document.getElementById("copyInchi");
  const copyInchikeyBtn = document.getElementById("copyInchikey");

  // State
  let currentTab = "iupac"; // 'iupac' | 'smiles' | 'cas'
  let full = false;
  let rows = []; // [{input, name, smiles, cas, inchi, inchikey, status, error}]

  // Utils
  const escapeHtml = (s) => (s ?? "").toString()
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#039;");

  function badge(st, err) {
    if (st === "done") return `<span class="badge success" title="OK">✓ Done</span>`;
    if (st === "loading") return `<span class="badge" title="Working…">… Processing</span>`;
    if (st === "error") return `<span class="badge error" title="${escapeHtml(err||'Unknown error')}">⚠︎ Error</span>`;
    return `<span class="badge" title="Idle">Idle</span>`;
  }

  function setPlaceholder() {
    if (currentTab === "iupac") inputArea.placeholder = "Paste IUPAC names here, one per line…";
    if (currentTab === "smiles") inputArea.placeholder = "Paste SMILES here, one per line…";
    if (currentTab === "cas") inputArea.placeholder = "Paste CAS numbers here, one per line…";
  }

  // Tabs
  tabs.addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    for (const t of tabs.querySelectorAll(".tab")) t.classList.remove("active");
    btn.classList.add("active");
    currentTab = btn.dataset.tab;
    setPlaceholder();
  });

  // Full conversion toggle (show/hide columns & copy buttons)
  fullToggle.addEventListener("change", () => {
    full = fullToggle.checked;
    const inchiHead = table.querySelector("th.col-inchi");
    const inchikeyHead = table.querySelector("th.col-inchikey");
    inchiHead.style.display = full ? "" : "none";
    inchikeyHead.style.display = full ? "" : "none";
    copyInchiBtn.style.display = full ? "" : "none";
    copyInchikeyBtn.style.display = full ? "" : "none";
    for (const td of table.querySelectorAll("td.col-inchi, td.col-inchikey")) {
      td.style.display = full ? "" : "none";
    }
  });

  // Wide mode toggle
  wideToggle.addEventListener("change", () => {
    document.body.classList.toggle("wide", wideToggle.checked);
  });

  // Build a table row element
  function makeRow(r, idx) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${idx+1}</td>
      <td>${escapeHtml(r.input ?? "")}</td>
      <td>${escapeHtml(r.name ?? "")}</td>
      <td class="mono col-smiles">${escapeHtml(r.smiles ?? "")}</td>
      <td>${escapeHtml(r.cas ?? "")}</td>
      <td class="col-inchi mono" style="${full ? "" : "display:none"}">${escapeHtml(r.inchi ?? "")}</td>
      <td class="col-inchikey mono" style="${full ? "" : "display:none"}">${escapeHtml(r.inchikey ?? "")}</td>
      <td>${badge(r.status, r.error)}</td>
    `;
    return tr;
  }

  function render() {
    resultsBody.innerHTML = "";
    rows.forEach((r, i) => resultsBody.appendChild(makeRow(r, i)));
  }

  function setProgress(done, total) {
    const pct = total ? Math.round((done/total)*100) : 0;
    progressBar.style.width = `${pct}%`;
    stats.textContent = total
      ? `Processed: ${done}/${total} (${pct}%). Remaining: ${Math.max(0, total - done)}.`
      : "Waiting…";
  }

  async function processOne(input, index) {
    rows[index].status = "loading";
    render();
    try {
      const body = { inputType: currentTab, value: input, fullConversion: full };

      // IMPORTANT FOR POSIT CONNECT: relative path (prefix-safe) + explicit error handling
      const res = await fetch("./resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
      const data = await res.json();

      rows[index].name     = data.name     || rows[index].name     || "";
      rows[index].smiles   = data.smiles   || rows[index].smiles   || "";
      rows[index].cas      = data.cas      || rows[index].cas      || "";
      rows[index].inchi    = data.inchi    || rows[index].inchi    || "";
      rows[index].inchikey = data.inchikey || rows[index].inchikey || "";

      if (data.error) {
        rows[index].status = "error";
        rows[index].error = data.error;
      } else {
        rows[index].status = "done";
        rows[index].error = "";
      }
    } catch (err) {
      rows[index].status = "error";
      rows[index].error = (err && err.message) ? err.message : "Request failed";
    }
    render();
  }

  async function runAll() {
    const inputs = inputArea.value
      .split(/\r?\n/)
      .map(s => s.trim())
      .filter(Boolean);

    if (!inputs.length) {
      stats.textContent = "Nothing to process.";
      return;
    }

    rows = inputs.map(i => ({
      input: i, name: "", smiles: "", cas: "", inchi: "", inchikey: "", status: "idle", error: ""
    }));
    render();
    setProgress(0, rows.length);

    const concurrency = 4;
    let completed = 0;
    async function worker(start) {
      for (let i = start; i < rows.length; i += concurrency) {
        await processOne(rows[i].input, i);
        completed += 1;
        setProgress(completed, rows.length);
      }
    }
    await Promise.all([...Array(concurrency)].map((_, k) => worker(k)));
  }

  runBtn.addEventListener("click", runAll);

  // Copy column helper (excludes headers)
  async function copyColumn(key) {
    const fillMissing = document.getElementById('missingAsNotFound')?.checked;
    const values = rows.map(r => {
      let v = (r[key] ?? "");
      if (!v && fillMissing) v = "Not Found";
      return v;
    });
    await navigator.clipboard.writeText(values.join("\n"));
  }
  document.querySelectorAll(".copy-btn").forEach(btn => {
    btn.addEventListener("click", () => copyColumn(btn.dataset.col));
  });

  // Defaults
  setPlaceholder();
})();

