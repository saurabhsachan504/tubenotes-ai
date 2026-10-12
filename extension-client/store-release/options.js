const $ = (id) => document.getElementById(id);

function showMsg(text, ok = true) {
  const el = $("msg");
  el.textContent = text;
  el.className = `msg ${ok ? "ok" : "err"}`;
}

async function restore() {
  const s = await browser.storage.sync.get([
    "apiUrl",
    "notebookId",
    "transformationId",
    "quickTransformationId",
    "chatModelId",
    "fastMode",
    "streamSummary",
    "summaryInstruction",
    "twoStageSummary",
    "summaryInstructionShort",
    "notesConcurrency",
  ]);
  if (s.apiUrl) $("apiUrl").value = s.apiUrl;
  window._savedNotebookId = s.notebookId;
  window._savedTransformationId = s.transformationId;
  window._savedQuickTransformationId = s.quickTransformationId || "";
  window._savedChatModelId = s.chatModelId || "";
  // Default ON when never set before.
  $("fastMode").checked = s.fastMode === undefined ? true : !!s.fastMode;
  $("streamSummary").checked = s.streamSummary === undefined ? true : !!s.streamSummary;
  $("twoStageSummary").checked = s.twoStageSummary === undefined ? true : !!s.twoStageSummary;
  $("summaryInstruction").value = s.summaryInstruction || "";
  $("summaryInstructionShort").value = s.summaryInstructionShort || "";
  $("notesConcurrency").value = s.notesConcurrency || 4;
}

async function loadOptions() {
  const apiUrl = $("apiUrl").value.replace(/\/$/, "");
  if (!apiUrl) {
    showMsg("Enter the API URL first.", false);
    return;
  }
  showMsg("Loading…");
  try {
    const [notebooks, transformations, models] = await Promise.all([
      fetch(`${apiUrl}/api/notebooks`).then((r) => r.json()),
      fetch(`${apiUrl}/api/transformations`).then((r) => r.json()),
      fetch(`${apiUrl}/api/models`).then((r) => r.json()),
    ]);

    const notebookSel = $("notebook");
    notebookSel.innerHTML = "";
    notebooks.forEach((nb) => {
      const opt = document.createElement("option");
      opt.value = nb.id;
      opt.textContent = nb.name;
      if (nb.id === window._savedNotebookId) opt.selected = true;
      notebookSel.appendChild(opt);
    });

    const transformSel = $("transformation");
    transformSel.innerHTML = "";
    transformations.forEach((tr) => {
      const opt = document.createElement("option");
      opt.value = tr.id;
      opt.textContent = tr.name || tr.title;
      if (tr.id === window._savedTransformationId) opt.selected = true;
      transformSel.appendChild(opt);
    });

    const quickSel = $("quickTransformation");
    quickSel.innerHTML =
      '<option value="">Not set — falls back to slower chat-based summary</option>';
    transformations.forEach((tr) => {
      const opt = document.createElement("option");
      opt.value = tr.id;
      opt.textContent = tr.name || tr.title;
      if (tr.id === window._savedQuickTransformationId) opt.selected = true;
      quickSel.appendChild(opt);
    });

    const chatModelSel = $("chatModel");
    chatModelSel.innerHTML = '<option value="">System default</option>';
    models
      .filter((m) => m.type === "language")
      .forEach((m) => {
        const opt = document.createElement("option");
        opt.value = m.id;
        opt.textContent = `${m.name} (${m.provider})`;
        if (m.id === window._savedChatModelId) opt.selected = true;
        chatModelSel.appendChild(opt);
      });

    showMsg(
      `Loaded ${notebooks.length} notebook(s), ${transformations.length} transformation(s), ${models.length} model(s). Pick your choices, then Save.`
    );
  } catch (err) {
    showMsg(`Couldn't load: ${err.message}`, false);
  }
}

async function save() {
  const apiUrl = $("apiUrl").value.replace(/\/$/, "");
  const notebookId = $("notebook").value;
  const transformationId = $("transformation").value;
  const quickTransformationId = $("quickTransformation").value || null;
  const chatModelId = $("chatModel").value || null;
  const fastMode = $("fastMode").checked;
  const streamSummary = $("streamSummary").checked;
  const summaryInstruction = $("summaryInstruction").value.trim() || null;
  const twoStageSummary = $("twoStageSummary").checked;
  const summaryInstructionShort = $("summaryInstructionShort").value.trim() || null;
  // Clamped: a silly value here would either serialise the whole job (0) or
  // flood Ollama with requests it will just queue anyway.
  const notesConcurrency = Math.min(8, Math.max(1, parseInt($("notesConcurrency").value, 10) || 4));

  if (!apiUrl || !notebookId || !transformationId) {
    showMsg("Load and select a notebook + transformation first.", false);
    return;
  }

  await browser.storage.sync.set({
    apiUrl,
    notebookId,
    transformationId,
    quickTransformationId,
    chatModelId,
    fastMode,
    streamSummary,
    summaryInstruction,
    twoStageSummary,
    summaryInstructionShort,
    notesConcurrency,
  });
  showMsg("Saved! You can close this tab.");
}

document.addEventListener("DOMContentLoaded", restore);
$("loadBtn").addEventListener("click", loadOptions);
$("saveBtn").addEventListener("click", save);
