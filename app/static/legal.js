(function () {
  const root = document.documentElement;
  const button = document.getElementById("legalThemeBtn");

  function applyTheme(theme, save) {
    const nextTheme = theme === "dark" ? "dark" : "light";
    root.dataset.theme = nextTheme;
    if (save) {
      try { localStorage.setItem("tn_theme", nextTheme); } catch (_) { /* Storage may be unavailable. */ }
    }
    if (button) {
      const switchTo = nextTheme === "dark" ? "light" : "dark";
      button.textContent = switchTo === "dark" ? "Dark" : "Light";
      button.setAttribute("aria-label", `Switch to ${switchTo} theme`);
      button.title = `Switch to ${switchTo} theme`;
    }
  }

  let savedTheme = "light";
  try { savedTheme = localStorage.getItem("tn_theme") || root.dataset.theme || "light"; } catch (_) { savedTheme = root.dataset.theme || "light"; }
  applyTheme(savedTheme, false);

  if (button) {
    button.addEventListener("click", () => {
      applyTheme(root.dataset.theme === "dark" ? "light" : "dark", true);
    });
  }

  const form = document.getElementById("contactForm");
  const formStatus = document.getElementById("contactFormStatus");
  const submitButton = document.getElementById("contactSubmitButton");
  if (form && formStatus && submitButton) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const payload = Object.fromEntries(data.entries());
      submitButton.disabled = true;
      formStatus.textContent = "Sending your message...";
      formStatus.dataset.state = "loading";
      try {
        const response = await fetch("/api/v1/contact", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.detail || "We could not send your message. Please try again.");
        form.reset();
        formStatus.textContent = result.detail || "Your message has been sent.";
        formStatus.dataset.state = "success";
      } catch (error) {
        formStatus.textContent = error.message || "We could not send your message. Please try again.";
        formStatus.dataset.state = "error";
      } finally {
        submitButton.disabled = false;
      }
    });
  }
}());
