// Very small markdown -> HTML converter, good enough for the transformation
// prompt's structure (##/### headings, bullet lists, plain paragraphs).
function mdToHtml(md) {
  const lines = md.split("\n");
  let html = "";
  let listTag = null; // "ul" | "ol" | null

  const closeList = () => {
    if (listTag) {
      html += `</${listTag}>`;
      listTag = null;
    }
  };
  const openList = (tag) => {
    if (listTag !== tag) {
      closeList();
      html += `<${tag}>`;
      listTag = tag;
    }
  };

  for (let raw of lines) {
    const line = raw.trim();
    if (!line) {
      closeList();
      continue;
    }
    if (line.startsWith("### ")) {
      closeList();
      const text = line.slice(4);
      const cls = /quick recap|^recap$/i.test(text) ? ' class="recap"' : "";
      html += `<h3${cls}>${escapeHtml(text)}</h3>`;
    } else if (line.startsWith("## ")) {
      closeList();
      html += `<h2>${escapeHtml(line.slice(3))}</h2>`;
    } else if (line.startsWith("# ")) {
      closeList();
      html += `<h2>${escapeHtml(line.slice(2))}</h2>`;
    } else if (/^\d+[.)]\s+/.test(line)) {
      openList("ol");
      html += `<li>${escapeHtml(line.replace(/^\d+[.)]\s+/, ""))}</li>`;
    } else if (/^[-*]\s+/.test(line)) {
      openList("ul");
      html += `<li>${escapeHtml(line.replace(/^[-*]\s+/, ""))}</li>`;
    } else {
      closeList();
      html += `<p>${escapeHtml(line)}</p>`;
    }
  }
  closeList();
  return html;
}

function escapeHtml(s) {
  let out = s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  out = out.replace(/==(.+?)==/g, '<mark class="hl">$1</mark>');
  out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  return out;
}

async function init() {
  const params = new URLSearchParams(location.search);
  const printJobId = params.get("printJobId");
  const { printJobs = {} } = await browser.storage.local.get("printJobs");
  const job = printJobs[printJobId];

  if (!job) {
    document.getElementById("title").textContent = "Summary not found";
    return;
  }

  // If the notes begin with a "# Title" line, use it for the cover heading and
  // drop it from the body so the title isn't repeated.
  let content = job.content || "";
  let heading = job.title;
  const m = content.match(/^\s*#\s+(.+)\s*(?:\n|$)/);
  if (m) {
    heading = m[1].trim();
    content = content.slice(m[0].length);
  }

  document.getElementById("title").textContent = heading;
  document.getElementById("meta").innerHTML =
    `🔗 <a href="${job.url}" target="_blank">${job.url}</a>`;
  document.getElementById("content").innerHTML = mdToHtml(content);

  document.title = job.title;

  // Give the browser a beat to lay out fonts (esp. Devanagari) before printing.
  setTimeout(() => window.print(), 400);
}

init();
