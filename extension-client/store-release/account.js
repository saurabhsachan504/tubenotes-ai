/** account.js — the toolbar popup: sign up / sign in / trial status / upgrade. */

const $ = (id) => document.getElementById(id);
let mode = "login";
let billingPlan = null;
let issuedCoupon = null;
let offerTimer = null;

// The first install opens account.html in a normal tab because Chrome does
// not allow extensions to programmatically open the toolbar popup.  Keep the
// same Google sign-in, but make it clear that this is the one-time setup step.
const isWelcomeInstall = new URLSearchParams(location.search).get("welcome") === "1";
if (isWelcomeInstall) {
  document.documentElement.classList.add("welcome-install");
  document.title = "Welcome to TubeNotes";
  $("welcomeTitle").innerHTML = 'Welcome to <span>TubeNotes</span>';
  $("welcomeCopy").textContent = "Turn YouTube videos into clear summaries, key points and PDF notes.";
  $("closeAuth").textContent = "Maybe later";
}

function formatPrice(plan) {
  if (!plan || typeof plan.price_cents !== "number") return "";
  const amount = plan.price_cents / 100;
  if (plan.currency === "INR") return `₹${amount}`;
  if (plan.currency === "USD") return `$${amount.toFixed(amount % 1 ? 2 : 0)}`;
  return `${plan.currency} ${amount.toFixed(2)}`;
}

function setUpgradeLabel() {
  const price = formatPrice(billingPlan);
  $("upgrade").textContent = price ? `Subscribe — ${price}/month` : "Subscribe";
  $("offerRegular").textContent = price
    ? `No thanks — continue at ${price}/month`
    : "No thanks — continue with regular price";
  $("offerOld").textContent = price || "Regular price";
}

function stopOfferTimer() {
  if (offerTimer) clearInterval(offerTimer);
  offerTimer = null;
}

function startOfferTimer(endsAt, serverTime) {
  stopOfferTimer();
  const remainingAtLoad = new Date(endsAt).getTime() - new Date(serverTime).getTime();
  const start = performance.now();
  const render = () => {
    const remaining = Math.max(0, remainingAtLoad - (performance.now() - start));
    if (!remaining) {
      $("offerCountdown").textContent = "Offer ended";
      $("offerClaim").disabled = true;
      stopOfferTimer();
      return;
    }
    const seconds = Math.floor(remaining / 1000);
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    $("offerCountdown").textContent = `${days}d : ${String(hours).padStart(2, "0")}h : ${String(minutes).padStart(2, "0")}m : ${String(secs).padStart(2, "0")}s`;
  };
  render();
  offerTimer = setInterval(render, 1000);
}

function paintOfferPrice(offer) {
  const price = formatPrice(offer);
  $("offerTitle").textContent = `Unlock Pro for ${price}`;
  $("offerNow").textContent = price;
  $("offerClaim").textContent = `Claim ${price}/month offer`;
}

function closeOffer() {
  stopOfferTimer();
  issuedCoupon = null;
  $("offerCard").classList.add("hidden");
  $("couponPanel").classList.add("hidden");
  $("offerClaim").classList.remove("hidden");
  $("offerErr").textContent = "";
}

async function openOffer() {
  $("offerErr").textContent = "";
  $("offerClaim").disabled = false;
  $("couponPanel").classList.add("hidden");
  $("offerClaim").classList.remove("hidden");
  $("offerCard").classList.remove("hidden");
  $("upgrade").classList.add("hidden");
  try {
    const offer = await TrialGuard.getLaunchOffer();
    paintOfferPrice(offer);
    startOfferTimer(offer.ends_at, offer.server_time);
    $("offerClaim").focus();
  } catch (err) {
    closeOffer();
    $("upgrade").classList.remove("hidden");
    throw err;
  }
}

function showCoupon(coupon) {
  issuedCoupon = coupon;
  paintOfferPrice(coupon);
  $("couponCode").textContent = coupon.code;
  $("couponInput").value = coupon.code;
  const expiry = new Date(coupon.expires_at);
  $("couponExpiry").textContent = Number.isNaN(expiry.getTime())
    ? "This code works only on this account."
    : `Valid until ${expiry.toLocaleString()}. This code works only on this account.`;
  $("offerClaim").classList.add("hidden");
  $("couponPanel").classList.remove("hidden");
  $("couponInput").focus();
}

async function openCheckout(couponCode) {
  const buttons = [$("upgrade"), $("offerClaim"), $("offerRegular"), $("couponApply")];
  buttons.forEach((button) => { button.disabled = true; });
  $("accountErr").textContent = "";
  $("offerErr").textContent = "";
  try {
    await TrialGuard.startCheckout(couponCode);
    window.close();
  } catch (err) {
    const message = err && err.status === 409 ? "You already have an active subscription." : describe(err);
    $("accountErr").textContent = message;
    if (!$("offerCard").classList.contains("hidden")) $("offerErr").textContent = message;
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

function setMode(next) {
  mode = next;
  const isSignup = next === "signup";
  $("tabLogin").classList.toggle("active", !isSignup);
  $("tabSignup").classList.toggle("active", isSignup);
  $("nameField").classList.toggle("hidden", !isSignup);
  $("signupHint").classList.toggle("hidden", !isSignup);
  $("authSubmit").textContent = isSignup ? "Create account" : "Sign in";
  $("password").setAttribute("autocomplete", isSignup ? "new-password" : "current-password");
  $("authErr").textContent = "";
  $("authErr").className = "err";
}

function describe(err) {
  if (!err) return "Something went wrong.";
  // A network failure is not the API saying no - it never got there at all.
  // Show the real reason and the URLs we tried, otherwise there is nothing
  // to act on.
  if (err.name === "NetworkError") return err.message;
  if (err.name !== "ApiError") return `Can't reach the account server. (${err.message || err})`;
  if (err.status === 429) return "Too many attempts. Please wait a few minutes.";
  return err.message;
}

// ---------------------------------------------------------------------------
$("tabLogin").addEventListener("click", () => setMode("login"));
$("tabSignup").addEventListener("click", () => setMode("signup"));
$("closeAuth").addEventListener("click", () => window.close());

$("googleSignIn").addEventListener("click", async () => {
  const btn = $("googleSignIn");
  const err = $("authErr");
  err.textContent = "";
  err.className = "err";
  btn.disabled = true;
  btn.textContent = "Opening Google…";
  try {
    await TrialGuard.googleLogin();
    TrialGuard.refreshBadge();
    await showAccount();
  } catch (e) {
    err.textContent = describe(e);
  } finally {
    btn.disabled = false;
    btn.textContent = "Continue with Google";
  }
});

$("authForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("authSubmit");
  const err = $("authErr");
  err.textContent = "";
  err.className = "err";
  btn.disabled = true;

  try {
    const email = $("email").value.trim();
    const password = $("password").value;
    if (mode === "signup") {
      await TrialGuard.signup(email, password, $("fullName").value.trim());
    } else {
      await TrialGuard.login(email, password);
    }
    TrialGuard.refreshBadge();
    await showAccount();
  } catch (e2) {
    err.textContent = describe(e2);
  } finally {
    btn.disabled = false;
  }
});

$("forgot").addEventListener("click", async () => {
  const email = $("email").value.trim();
  const err = $("authErr");
  if (!email) {
    err.textContent = "Enter your email first.";
    return;
  }
  try {
    const res = await TrialGuard.forgotPassword(email);
    err.textContent = res.detail;
    err.className = "err ok";
  } catch (e) {
    err.textContent = describe(e);
  }
});

$("testConn").addEventListener("click", async () => {
  const err = $("authErr");
  err.className = "err";
  err.textContent = "Checking…";
  const r = await TrialGuard.ping();
  if (r.ok) {
    err.className = "err ok";
    err.textContent = `Server reachable at ${r.base.replace(/\/api\/v1$/, "")} ✓`;
  } else {
    const where = r.tried.map((b) => b.replace(/\/api\/v1$/, ""));
    // Telling someone to run uvicorn only makes sense on a dev machine. For
    // the live server that advice is nonsense and hides the real problem.
    const hint = /\/\/(localhost|127\.0\.0\.1)/.test(r.tried[0])
      ? "Is the backend running? (uvicorn app.main:app --reload)"
      : "Check your internet connection, or try again in a minute.";
    err.className = "err";
    err.textContent = `No answer from ${where.join(" or ")}. ${hint}`;
  }
});

$("signout").addEventListener("click", async () => {
  await TrialGuard.logout();
  TrialGuard.refreshBadge();
  $("accountView").classList.add("hidden");
  $("authView").classList.remove("hidden");
  setMode("login");
});

$("upgrade").addEventListener("click", async () => {
  try {
    // Only the server-selected INR tier receives the discounted offer. The
    // backend checks the country and coupon again before it creates checkout.
    if (billingPlan && billingPlan.currency === "INR") await openOffer();
    else await openCheckout("");
  } catch (err) {
    $("accountErr").textContent = describe(err);
  }
});

$("offerClose").addEventListener("click", () => {
  closeOffer();
  $("upgrade").classList.remove("hidden");
});

$("offerClaim").addEventListener("click", async () => {
  const button = $("offerClaim");
  button.disabled = true;
  $("offerErr").textContent = "";
  try {
    showCoupon(await TrialGuard.claimLaunchOffer());
  } catch (err) {
    $("offerErr").textContent = describe(err);
  } finally {
    button.disabled = false;
  }
});

$("offerRegular").addEventListener("click", () => openCheckout(""));
$("couponApply").addEventListener("click", () => {
  const code = $("couponInput").value.trim();
  // Never fall through to normal checkout here. A missing/incorrect code must
  // remain on this card so the customer cannot be charged the regular price by
  // mistake after choosing the offer.
  if (!issuedCoupon || !code || code.toUpperCase() !== issuedCoupon.code.toUpperCase()) {
    $("offerErr").textContent = "Paste your personal coupon code before continuing.";
    $("couponInput").focus();
    return;
  }
  openCheckout(code);
});
$("couponInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); $("couponApply").click(); }
});
$("couponCopy").addEventListener("click", async () => {
  if (!issuedCoupon) return;
  try {
    await navigator.clipboard.writeText(issuedCoupon.code);
    $("couponCopy").textContent = "Copied";
    setTimeout(() => { $("couponCopy").textContent = "Copy"; }, 1400);
  } catch (_) {
    $("offerErr").textContent = "Select and copy the code manually.";
  }
});

$("manage").addEventListener("click", async () => {
  $("accountErr").textContent = "";
  try {
    await TrialGuard.openBillingPortal();
    window.close();
  } catch (e) {
    $("accountErr").textContent =
      e.status === 404 ? "No billing portal for this account yet." : describe(e);
  }
});

// ---------------------------------------------------------------------------
function paint(ent) {
  const isPro = ent.plan === "subscription";
  const left = Math.min(ent.trials_remaining, ent.device_trials_remaining);
  const used = ent.trials_limit - left;

  $("planBadge").textContent = isPro ? "Pro" : "Free";
  $("planBadge").classList.toggle("pro", isPro);
  $("upgrade").classList.toggle("hidden", isPro);
  $("manage").classList.toggle("hidden", !isPro);
  if (isPro) closeOffer();

  if (isPro) {
    $("statusLine").textContent = ent.subscription_status === "manual"
      ? "Complimentary Pro active"
      : "Subscription active";
    $("meterFill").style.width = "100%";
    $("detailLine").textContent = ent.current_period_end
      ? `Renews ${new Date(ent.current_period_end).toLocaleDateString()}`
      : (ent.subscription_status === "manual"
        ? "Complimentary Pro access"
        : `Billed ${formatPrice(billingPlan) || "monthly"}`);
    return;
  }

  $("statusLine").textContent =
    left > 0 ? `${left} of ${ent.trials_limit} free videos left` : "Free videos used up";
  $("meterFill").style.width = `${(used / ent.trials_limit) * 100}%`;

  const notes = {
    device_trial_exhausted: "The free videos for this device have already been used.",
    machine_trial_exhausted: "This computer has used up its free videos.",
    email_verification_required: "Verify your email to start your free videos.",
    account_disabled: "This account is disabled.",
    device_blocked: "This device has been blocked.",
  };
  $("detailLine").textContent =
    notes[ent.reason] ||
    "One summary = one video. Re-opening a video you already summarized is free.";
}

async function showAccount() {
  $("authView").classList.add("hidden");
  $("accountView").classList.remove("hidden");

  // Paint the cached state first so the popup is never blank.
  const cached = await TrialGuard.getCachedEntitlement();
  if (cached) paint(cached);

  const [user, ent, plan] = await Promise.all([
    TrialGuard.me().catch(() => null),
    TrialGuard.checkEntitlement().catch(() => null),
    TrialGuard.getBillingPrice().catch(() => null),
  ]);
  billingPlan = plan || billingPlan;
  setUpgradeLabel();
  if (user) $("emailLine").textContent = user.email;
  if (ent) paint(ent);
  else if (!cached) $("accountErr").textContent = "Can't reach the account server.";
}

(async function boot() {
  setMode("login");
  if (await TrialGuard.isSignedIn()) await showAccount();
})();
