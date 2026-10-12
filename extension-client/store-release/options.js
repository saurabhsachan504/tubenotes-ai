document.addEventListener("DOMContentLoaded", () => {
  const account = document.getElementById("openAccount");
  if (account) {
    account.addEventListener("click", () => {
      browser.tabs.create({ url: browser.runtime.getURL("account.html") });
    });
  }
});
