/* TubeNotes — front-end for the YouTube summarizer web app.
   Talks to the same FastAPI service that serves this page, so there is no CORS
   and no separate API host to configure. */
(function () {
  "use strict";

  const API = "/api/v1";
  const $ = (id) => document.getElementById(id);
  const TOK = "tn_tokens";
  const DEV = "tn_device";

  let mode = "summary";
  let lastNotes = null;   // { videoId, title, url, markdown, lang }
  let lastTranscript = null; // { videoId, text, lang }; reused by Full Notes
  let videoChat = { context: null, history: [], busy: false };
  let busy = false;
  // Empty on the primary site. A legacy domain can set this through /meta so
  // its Subscribe button takes users to the canonical checkout domain.
  let billingPrimarySiteUrl = "";

  // Offered output languages. "auto" keeps the video's own language, which is
  // the default because that is what most people want.
  const LANGS = [
    ["hi","हिन्दी — Hindi"],["en","English"],["mr","मराठी — Marathi"],
    ["gu","ગુજરાતી — Gujarati"],["bn","বাংলা — Bengali"],["pa","ਪੰਜਾਬੀ — Punjabi"],
    ["ta","தமிழ் — Tamil"],["te","తెలుగు — Telugu"],["kn","ಕನ್ನಡ — Kannada"],
    ["ml","മലയാളം — Malayalam"],["or","ଓଡ଼ିଆ — Odia"],["as","অসমীয়া — Assamese"],
    ["ur","اردو — Urdu"],["ne","नेपाली — Nepali"],["sa","संस्कृतम् — Sanskrit"],
    ["es","Español"],["fr","Français"],["de","Deutsch"],["pt","Português"],
    ["it","Italiano"],["nl","Nederlands"],["ru","Русский"],["uk","Українська"],
    ["ar","العربية"],["fa","فارسی"],["tr","Türkçe"],["he","עברית"],
    ["zh","中文"],["ja","日本語"],["ko","한국어"],["th","ไทย"],["vi","Tiếng Việt"],
    ["id","Bahasa Indonesia"],["ms","Bahasa Melayu"],["pl","Polski"],["ro","Română"],
    ["el","Ελληνικά"],["sv","Svenska"],["cs","Čeština"],["hu","Magyar"],
    ["fi","Suomi"],["da","Dansk"],["no","Norsk"],["sw","Kiswahili"],["si","සිංහල"],
  ];

  // A user does not need to find the dropdown to translate a summary. These
  // aliases deliberately accept common Hindi/Hinglish spellings too.
  const CHAT_LANGUAGE_ALIASES = [
    ["en", ["english", "अंग्रेजी", "अंग्रेज़ी"]], ["hi", ["hindi", "हिंदी", "हिन्दी"]],
    ["bn", ["bengali", "bangla", "बंगाली", "বাংলা"]], ["ta", ["tamil", "तमिल", "தமிழ்"]],
    ["gu", ["gujarati", "gujrati", "गुजराती", "ગુજરાતી"]], ["mr", ["marathi", "मराठी"]],
    ["pa", ["punjabi", "पंजाबी", "ਪੰਜਾਬੀ"]], ["te", ["telugu", "तेलुगु", "తెలుగు"]],
    ["kn", ["kannada", "कन्नड़", "ಕನ್ನಡ"]], ["ml", ["malayalam", "मलयालम", "മലയാളം"]],
    ["ur", ["urdu", "उर्दू", "اردو"]], ["ne", ["nepali", "नेपाली"]],
    ["as", ["assamese", "असमिया"]], ["or", ["odia", "oriya", "ओड़िया"]],
    ["sa", ["sanskrit", "संस्कृत"]], ["fr", ["french", "फ्रेंच", "français"]],
    ["es", ["spanish", "स्पेनिश", "español"]], ["de", ["german", "जर्मन", "deutsch"]],
    ["pt", ["portuguese", "पुर्तगाली"]], ["it", ["italian", "इटालियन"]],
    ["nl", ["dutch", "डच"]], ["ru", ["russian", "रूसी", "русский"]],
    ["ar", ["arabic", "अरबी", "العربية"]], ["fa", ["persian", "farsi", "फारसी"]],
    ["tr", ["turkish", "तुर्की"]], ["zh", ["chinese", "चीनी", "中文"]],
    ["ja", ["japanese", "जापानी", "日本語"]], ["ko", ["korean", "कोरियाई", "한국어"]],
  ];

  // The chat starter is rendered locally, so it should use the exact output
  // language without making a second translation request.
  const CHAT_STARTER_COPY = {
    en: ["Want to explore this summary further?", "What are the key takeaways?", "Explain this simply", "What should I remember?", "Ask a question…"],
    hi: ["इस सारांश को और समझना चाहते हैं?", "मुख्य बातें क्या हैं?", "इसे सरल भाषा में समझाइए", "मुझे क्या याद रखना चाहिए?", "कोई प्रश्न पूछें…"],
    mr: ["या सारांशाचा अधिक शोध घ्यायचा आहे?", "मुख्य मुद्दे कोणते आहेत?", "हे सोप्या भाषेत समजावून सांगा", "मला काय लक्षात ठेवावे?", "प्रश्न विचारा…"],
    gu: ["આ સારાંશને વધુ સમજવા માંગો છો?", "મુખ્ય મુદ્દાઓ કયા છે?", "આને સરળ રીતે સમજાવો", "મારે શું યાદ રાખવું જોઈએ?", "પ્રશ્ન પૂછો…"],
    bn: ["এই সারাংশটি আরও জানতে চান?", "মূল বিষয়গুলো কী?", "এটি সহজভাবে ব্যাখ্যা করুন", "আমার কী মনে রাখা উচিত?", "একটি প্রশ্ন করুন…"],
    pa: ["ਕੀ ਤੁਸੀਂ ਇਸ ਸਾਰਾਂਸ਼ ਨੂੰ ਹੋਰ ਸਮਝਣਾ ਚਾਹੁੰਦੇ ਹੋ?", "ਮੁੱਖ ਨੁਕਤੇ ਕੀ ਹਨ?", "ਇਸ ਨੂੰ ਸੌਖੇ ਤਰੀਕੇ ਨਾਲ ਸਮਝਾਓ", "ਮੈਨੂੰ ਕੀ ਯਾਦ ਰੱਖਣਾ ਚਾਹੀਦਾ ਹੈ?", "ਸਵਾਲ ਪੁੱਛੋ…"],
    ta: ["இந்தச் சுருக்கத்தை மேலும் அறிய விரும்புகிறீர்களா?", "முக்கிய கருத்துகள் என்ன?", "இதை எளிமையாக விளக்கவும்", "நான் எதை நினைவில் வைத்துக்கொள்ள வேண்டும்?", "ஒரு கேள்வியைக் கேளுங்கள்…"],
    te: ["ఈ సారాంశాన్ని మరింత తెలుసుకోవాలనుకుంటున్నారా?", "ముఖ్యాంశాలు ఏమిటి?", "దీన్ని సులభంగా వివరించండి", "నేను ఏమి గుర్తుంచుకోవాలి?", "ఒక ప్రశ్న అడగండి…"],
    kn: ["ಈ ಸಾರಾಂಶವನ್ನು ಇನ್ನಷ್ಟು ತಿಳಿದುಕೊಳ್ಳಲು ಬಯಸುವಿರಾ?", "ಮುಖ್ಯ ಅಂಶಗಳೇನು?", "ಇದನ್ನು ಸರಳವಾಗಿ ವಿವರಿಸಿ", "ನಾನು ಏನನ್ನು ನೆನಪಿಟ್ಟುಕೊಳ್ಳಬೇಕು?", "ಒಂದು ಪ್ರಶ್ನೆ ಕೇಳಿ…"],
    ml: ["ഈ സംഗ്രഹം കൂടുതൽ അറിയണോ?", "പ്രധാന കാര്യങ്ങൾ എന്തൊക്കെയാണ്?", "ഇത് ലളിതമായി വിശദീകരിക്കൂ", "ഞാൻ എന്ത് ഓർക്കണം?", "ഒരു ചോദ്യം ചോദിക്കൂ…"],
    or: ["ଏହି ସାରାଂଶ ବିଷୟରେ ଆହୁରି ଜାଣିବାକୁ ଚାହାଁନ୍ତି କି?", "ମୁଖ୍ୟ ବିଷୟଗୁଡ଼ିକ କ’ଣ?", "ଏହାକୁ ସରଳ ଭାବରେ ବୁଝାନ୍ତୁ", "ମୁଁ କ’ଣ ମନେ ରଖିବା ଉଚିତ?", "ଏକ ପ୍ରଶ୍ନ ପଚାରନ୍ତୁ…"],
    as: ["এই সাৰাংশটো আৰু জানিব বিচাৰেনে?", "মূল কথাবোৰ কি?", "ইয়াক সহজকৈ বুজাওক", "মই কি মনত ৰাখিব লাগে?", "এটা প্ৰশ্ন সোধক…"],
    ur: ["کیا آپ اس خلاصے کو مزید سمجھنا چاہتے ہیں؟", "اہم نکات کیا ہیں؟", "اسے آسان الفاظ میں سمجھائیں", "مجھے کیا یاد رکھنا چاہیے؟", "ایک سوال پوچھیں…"],
    ne: ["यो सारांश अझ बुझ्न चाहनुहुन्छ?", "मुख्य बुँदाहरू के हुन्?", "यसलाई सजिलो रूपमा बुझाउनुहोस्", "मैले के सम्झनुपर्छ?", "प्रश्न सोध्नुहोस्…"],
    sa: ["एतत् सारांशं अधिकं ज्ञातुम् इच्छसि वा?", "मुख्यबिन्दवः के सन्ति?", "एतत् सरलतया व्याख्यातु", "मया किं स्मर्तव्यम्?", "प्रश्नं पृच्छ…"],
    es: ["¿Quieres explorar más este resumen?", "¿Cuáles son las ideas clave?", "Explícalo de forma sencilla", "¿Qué debería recordar?", "Haz una pregunta…"],
    fr: ["Voulez-vous approfondir ce résumé ?", "Quels sont les points clés ?", "Expliquez ceci simplement", "Que dois-je retenir ?", "Posez une question…"],
    de: ["Möchten Sie diese Zusammenfassung weiter erkunden?", "Was sind die wichtigsten Erkenntnisse?", "Einfach erklären", "Was sollte ich mir merken?", "Stellen Sie eine Frage…"],
    pt: ["Quer explorar mais este resumo?", "Quais são os pontos principais?", "Explique isso de forma simples", "Do que devo me lembrar?", "Faça uma pergunta…"],
    it: ["Vuoi approfondire questo riepilogo?", "Quali sono i punti chiave?", "Spiegalo in modo semplice", "Cosa dovrei ricordare?", "Fai una domanda…"],
    nl: ["Wilt u deze samenvatting verder verkennen?", "Wat zijn de belangrijkste punten?", "Leg dit eenvoudig uit", "Wat moet ik onthouden?", "Stel een vraag…"],
    ru: ["Хотите подробнее изучить это резюме?", "Каковы главные выводы?", "Объясните это просто", "Что мне следует запомнить?", "Задайте вопрос…"],
    uk: ["Хочете дізнатися більше про цей підсумок?", "Які головні висновки?", "Поясніть це простіше", "Що мені слід запам’ятати?", "Поставте запитання…"],
    ar: ["هل تريد استكشاف هذا الملخص أكثر؟", "ما أهم النقاط؟", "اشرح هذا ببساطة", "ما الذي ينبغي أن أتذكره؟", "اطرح سؤالاً…"],
    fa: ["می‌خواهید این خلاصه را بیشتر بررسی کنید؟", "نکات کلیدی چیستند؟", "این را ساده توضیح دهید", "چه چیزی را باید به خاطر بسپارم؟", "یک سؤال بپرسید…"],
    tr: ["Bu özeti daha fazla incelemek ister misiniz?", "Temel çıkarımlar nelerdir?", "Bunu basitçe açıklayın", "Ne hatırlamalıyım?", "Bir soru sorun…"],
    he: ["רוצה להעמיק בסיכום הזה?", "מהן הנקודות העיקריות?", "הסבר זאת בפשטות", "מה כדאי לי לזכור?", "שאל שאלה…"],
    zh: ["想进一步了解这份摘要吗？", "关键要点是什么？", "请简单解释一下", "我应该记住什么？", "提出问题…"],
    ja: ["この要約をさらに詳しく見ますか？", "重要なポイントは何ですか？", "簡単に説明してください", "何を覚えておくべきですか？", "質問する…"],
    ko: ["이 요약을 더 자세히 살펴볼까요?", "핵심 내용은 무엇인가요?", "쉽게 설명해 주세요", "무엇을 기억해야 하나요?", "질문하기…"],
    th: ["ต้องการสำรวจสรุปนี้เพิ่มเติมไหม?", "ประเด็นสำคัญคืออะไร?", "อธิบายแบบเข้าใจง่าย", "ฉันควรจำอะไรไว้?", "ถามคำถาม…"],
    vi: ["Bạn muốn tìm hiểu thêm về bản tóm tắt này không?", "Những ý chính là gì?", "Giải thích điều này một cách đơn giản", "Tôi nên nhớ điều gì?", "Đặt câu hỏi…"],
    id: ["Ingin mempelajari ringkasan ini lebih lanjut?", "Apa poin-poin utamanya?", "Jelaskan ini dengan sederhana", "Apa yang harus saya ingat?", "Ajukan pertanyaan…"],
    ms: ["Ingin meneroka ringkasan ini dengan lebih lanjut?", "Apakah perkara pentingnya?", "Terangkan ini dengan mudah", "Apakah yang perlu saya ingat?", "Tanya soalan…"],
    pl: ["Chcesz lepiej poznać to podsumowanie?", "Jakie są najważniejsze wnioski?", "Wyjaśnij to prosto", "Co powinienem zapamiętać?", "Zadaj pytanie…"],
    ro: ["Doriți să explorați mai mult acest rezumat?", "Care sunt ideile principale?", "Explicați simplu", "Ce ar trebui să rețin?", "Puneți o întrebare…"],
    el: ["Θέλετε να εξερευνήσετε περισσότερο αυτή τη σύνοψη;", "Ποια είναι τα βασικά σημεία;", "Εξηγήστε το απλά", "Τι πρέπει να θυμάμαι;", "Κάντε μια ερώτηση…"],
    sv: ["Vill du utforska den här sammanfattningen vidare?", "Vilka är de viktigaste punkterna?", "Förklara detta enkelt", "Vad bör jag komma ihåg?", "Ställ en fråga…"],
    cs: ["Chcete tento souhrn dále prozkoumat?", "Jaké jsou hlavní body?", "Vysvětlete to jednoduše", "Co si mám zapamatovat?", "Položte otázku…"],
    hu: ["Szeretné tovább felfedezni ezt az összefoglalót?", "Melyek a fő tanulságok?", "Magyarázza el egyszerűen", "Mire kell emlékeznem?", "Tegyen fel egy kérdést…"],
    fi: ["Haluatko tutkia tätä yhteenvetoa tarkemmin?", "Mitkä ovat keskeiset asiat?", "Selitä tämä yksinkertaisesti", "Mitä minun pitäisi muistaa?", "Esitä kysymys…"],
    da: ["Vil du udforske dette resumé yderligere?", "Hvad er de vigtigste pointer?", "Forklar dette enkelt", "Hvad skal jeg huske?", "Stil et spørgsmål…"],
    no: ["Vil du utforske dette sammendraget videre?", "Hva er hovedpoengene?", "Forklar dette enkelt", "Hva bør jeg huske?", "Still et spørsmål…"],
    sw: ["Je, unataka kuchunguza muhtasari huu zaidi?", "Mambo muhimu ni yapi?", "Eleza hili kwa urahisi", "Ninapaswa kukumbuka nini?", "Uliza swali…"],
    si: ["මෙම සාරාංශය තවදුරටත් ගවේෂණය කිරීමට අවශ්‍යද?", "ප්‍රධාන කරුණු මොනවාද?", "මෙය සරලව පැහැදිලි කරන්න", "මා මතක තබාගත යුත්තේ කුමක්ද?", "ප්‍රශ්නයක් අසන්න…"],
  };

  function fillLangSelect(el, { includeAuto }) {
    el.innerHTML = "";
    if (includeAuto) {
      const o = document.createElement("option");
      o.value = "auto"; o.textContent = "Same as the video";
      el.appendChild(o);
    } else {
      const o = document.createElement("option");
      o.value = ""; o.textContent = "🌐 Translate to…";
      el.appendChild(o);
    }
    for (const [code, name] of LANGS) {
      const o = document.createElement("option");
      o.value = code; o.textContent = name;
      el.appendChild(o);
    }
  }

  // Summary hamesha video ki apni bhasha me banti hai. Dropdown hata diya gaya
  // hai, isliye yahan element milta hi nahi - null ka matlab "auto" hai.
  // Dropdown wapas daalte hi ye phir se us ki value padhne lagega.
  const outLang = () => {
    const el = $("outLang");
    if (!el) return null;
    const v = el.value;
    return v && v !== "auto" ? v : null;
  };

  // =====================================================================
  // Device fingerprint — the server hashes this to enforce the free-trial
  // limit per machine. A browser cannot read a MAC address, so this is the
  // strongest identifier available; the server also keeps a hardware-only
  // ledger so clearing site data does not hand out fresh credits.
  // =====================================================================
  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`;
  }

  function gpu() {
    try {
      const gl = document.createElement("canvas").getContext("webgl");
      const ext = gl && gl.getExtension("WEBGL_debug_renderer_info");
      return ext ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)).slice(0, 200) : null;
    } catch (_) { return null; }
  }

  function device() {
    let rec = null;
    try { rec = JSON.parse(localStorage.getItem(DEV) || "null"); } catch (_) {}
    if (!rec || !rec.installation_id) rec = { installation_id: uuid() };
    if (!rec.gpu) rec.gpu = gpu();
    try { localStorage.setItem(DEV, JSON.stringify(rec)); } catch (_) {}

    const fp = {
      installation_id: rec.installation_id,
      platform: navigator.platform || (navigator.userAgentData && navigator.userAgentData.platform),
      user_agent_brand: (navigator.userAgentData && navigator.userAgentData.brands
        ? navigator.userAgentData.brands.map((b) => b.brand + " " + b.version).join("|")
        : navigator.userAgent || "").slice(0, 200),
      screen: `${screen.width}x${screen.height}x${screen.colorDepth}`,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      language: navigator.language,
      hardware_concurrency: navigator.hardwareConcurrency,
      device_memory: navigator.deviceMemory,
      gpu: rec.gpu,
    };
    const out = {};
    for (const k in fp) if (fp[k] !== undefined && fp[k] !== null && fp[k] !== "") out[k] = fp[k];
    return out;
  }

  // =====================================================================
  // Auth
  // =====================================================================
  const tokens = {
    get() { try { return JSON.parse(localStorage.getItem(TOK) || "null"); } catch (_) { return null; } },
    set(t) {
      localStorage.setItem(TOK, JSON.stringify({
        access_token: t.access_token,
        refresh_token: t.refresh_token,
        expires_at: Date.now() + (t.expires_in - 60) * 1000,
      }));
    },
    clear() { localStorage.removeItem(TOK); },
  };

  const signedIn = () => tokens.get() !== null;
  let refreshing = null;

  async function refreshTokens() {
    if (refreshing) return refreshing;
    refreshing = (async () => {
      const t = tokens.get();
      if (!t) return null;
      try {
        const r = await fetch(`${API}/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: t.refresh_token }),
        });
        if (!r.ok) { tokens.clear(); return null; }
        const fresh = await r.json();
        tokens.set(fresh);
        return fresh;
      } catch (_) { return null; }
      finally { refreshing = null; }
    })();
    return refreshing;
  }

  async function authHeader() {
    const t = tokens.get();
    if (!t) return null;
    if (Date.now() < t.expires_at) return `Bearer ${t.access_token}`;
    const fresh = await refreshTokens();
    return fresh ? `Bearer ${fresh.access_token}` : null;
  }

  async function api(path, opts) {
    opts = opts || {};
    const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers);
    if (opts.auth !== false) {
      const h = await authHeader();
      if (!h) throw err(401, "Not signed in");
      headers.Authorization = h;
    }
    const init = { method: opts.method || "GET", headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      cache: opts.cache, signal: opts.signal };

    let res = await fetch(API + path, init);
    if (res.status === 401 && opts.auth !== false) {
      const fresh = await refreshTokens();
      if (fresh) { headers.Authorization = `Bearer ${fresh.access_token}`; res = await fetch(API + path, init); }
    }
    if (opts.raw) return res;

    const text = await res.text();
    let json = null; try { json = text ? JSON.parse(text) : null; } catch (_) {}
    if (!res.ok) { if (res.status === 401) tokens.clear(); throw err(res.status, json ? json.detail : text); }
    return json;
  }

  function err(status, detail) {
    const e = new Error(typeof detail === "string" ? detail : (detail && detail.message) || "Request failed");
    e.status = status; e.detail = detail;
    e.entitlement = detail && detail.entitlement;
    return e;
  }

  // =====================================================================
  // Trials chip
  // =====================================================================
  function paintChip(ent) {
    const chip = $("trialChip");
    $("accountBtn").textContent = signedIn() ? "Account" : "Sign in";
    if (!signedIn() || !ent) { chip.classList.add("hidden"); return; }
    chip.classList.remove("hidden");
    chip.classList.remove("pro", "out");
    if (ent.plan === "subscription") { chip.classList.add("pro"); chip.textContent = "★ Pro"; return; }
    const left = Math.min(ent.trials_remaining, ent.device_trials_remaining);
    if (left <= 0) chip.classList.add("out");
    chip.textContent = `${left} free left`;
  }

  async function refreshEntitlement() {
    if (!signedIn()) { paintChip(null); return null; }
    try {
      const ent = await api("/entitlement/check", { method: "POST", body: { device: device() } });
      paintChip(ent);
      return ent;
    } catch (_) { paintChip(null); return null; }
  }

  // =====================================================================
  // Markdown (small, safe: escape first, then re-introduce structure)
  // =====================================================================
  function md2html(md) {
    const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const inline = (s) => esc(s)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?]|$)/g, "$1<em>$2</em>");
    let html = "", list = null;
    const close = () => { if (list) { html += `</${list}>`; list = null; } };
    for (const raw of String(md || "").split("\n")) {
      const line = raw.trim();
      if (!line) { close(); continue; }
      let m;
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        close();
        const lvl = Math.min(m[1].length, 3);
        html += `<h${lvl}>${inline(m[2])}</h${lvl}>`;
      } else if (/^\d+[.)]\s+/.test(line)) {
        if (list !== "ol") { close(); html += "<ol>"; list = "ol"; }
        html += `<li>${inline(line.replace(/^\d+[.)]\s+/, ""))}</li>`;
      } else if (/^[-*•]\s+/.test(line)) {
        if (list !== "ul") { close(); html += "<ul>"; list = "ul"; }
        html += `<li>${inline(line.replace(/^[-*•]\s+/, ""))}</li>`;
      } else {
        close();
        html += `<p>${inline(line)}</p>`;
      }
    }
    close();
    return html;
  }

  // =====================================================================
  // Result rendering
  // =====================================================================
  const R = () => $("result");

  function shell(video, extraTag) {
    const tags = [];
    if (video && video.author) tags.push(`<span class="tag">${escapeAttr(video.author)}</span>`);
    if (extraTag) tags.push(extraTag);
    R().classList.remove("hidden");
    R().innerHTML = `
      <div class="card">
        <div class="vidrow">
          <img id="rThumb" alt="" src="${video ? escapeAttr(video.thumbnail) : ""}"
               onerror="this.style.visibility='hidden'" onload="this.style.visibility='visible'" />
          <div>
            <div class="t" id="rTitle">${video ? escapeAttr(video.title) : "Loading…"}</div>
            <div class="m" id="rMeta">${tags.join("")}</div>
          </div>
        </div>
        <div class="toolbar" id="rTools"></div>
        <div class="body">
          <div id="rNote"></div>
          <div id="rPrint"></div>
          <div id="rProgress" class="hidden"><div class="progress"><i id="rBar"></i></div></div>
          <div id="rStatus" class="status"></div>
          <div class="md" id="rBody"></div>
        </div>
      </div>`;
    R().scrollIntoView({ behavior: "smooth", block: "start" });
  }

  /**
   * Paint the full-notes progress card before making any network request.
   * The first visible state is therefore immediate, even while /video/info or
   * the transcript provider is still working.
   */
  function showPdfProgress(video) {
    const title = video && video.title ? video.title : "Getting video details…";
    const thumbnail = video && video.thumbnail ? video.thumbnail : "";
    const author = video && video.author ? `<span class="tag">${escapeAttr(video.author)}</span>` : "";
    R().classList.remove("hidden");
    R().innerHTML = `
      <div class="card pdf-flow" id="pdfFlow">
        <div class="pdf-flow-grid">
          <div class="pdf-flow-video">
            <img id="rThumb" alt="" src="${escapeAttr(thumbnail)}"
                 onerror="this.style.visibility='hidden'"
                 onload="this.style.visibility='visible'" />
            <div>
              <div class="pdf-flow-title" id="rTitle">${escapeAttr(title)}</div>
              <div class="pdf-flow-meta" id="rMeta">${author}</div>
            </div>
          </div>
          <div class="pdf-flow-main">
            <div class="pdf-flow-head"><span class="pdf-flow-icon">${ICONS.pdf}</span><span id="pdfFlowTitle">Initializing PDF generation…</span></div>
            <div class="pdf-flow-copy" id="pdfFlowCopy">Reading the video and preparing your detailed notes. This may take a few moments.</div>
            <div class="pdf-flow-meter"><div class="progress"><i id="pdfFlowBar" style="width:4%"></i></div><span class="pdf-flow-percent" id="pdfFlowPercent">4%</span></div>
          </div>
          <div class="pdf-steps" aria-label="PDF generation progress">
            <div class="pdf-step active" data-pdf-step="1"><div class="pdf-step-dot">1</div>Reading<br>Video</div>
            <div class="pdf-step" data-pdf-step="2"><div class="pdf-step-dot">2</div>Analyzing<br>Content</div>
            <div class="pdf-step" data-pdf-step="3"><div class="pdf-step-dot">3</div>Writing<br>Notes</div>
            <div class="pdf-step" data-pdf-step="4"><div class="pdf-step-dot">4</div>Preparing<br>PDF</div>
          </div>
        </div>
        <!-- Below the steps, and fixed height: it fills as the notes are
             written, and putting it above pushed the bar and the step markers
             down the page as it grew. -->
        <div class="pdf-flow-live" id="pdfFlowLive" hidden></div>
        <div class="load-meter" id="loadMeter" hidden>
          <div class="load-meter-head">
            <span>Server load</span><span id="loadMeterText">checking…</span>
          </div>
          <div class="load-meter-bar"><i id="loadMeterFill" style="width:0%"></i></div>
        </div>
      </div>`;
    R().scrollIntoView({ behavior: "smooth", block: "center" });
  }

  let loadTimer = null;

  async function pollServerLoad() {
    const meter = $("loadMeter");
    if (!meter) return;
    try {
      const r = await fetch(`${API}/load`, { cache: "no-store" });
      if (!r.ok) return;
      const d = await r.json();
      const running = d.running == null ? null : Math.round(d.running);
      const busy = d.busy_percent == null ? 0 : d.busy_percent;
      meter.hidden = false;
      const fill = $("loadMeterFill");
      if (fill) {
        fill.style.width = `${Math.max(2, Math.min(100, busy))}%`;
        // Green under half, amber approaching full, red once queueing starts.
        fill.className = d.waiting > 0 ? "hot" : busy >= 70 ? "warm" : "";
      }
      const bits = [];
      if (running != null) bits.push(`${running}/${d.capacity} generating`);
      if (d.waiting) bits.push(`${Math.round(d.waiting)} queued`);
      if (d.jobs) bits.push(`${d.jobs} PDF${d.jobs === 1 ? "" : "s"}`);
      const txt = $("loadMeterText");
      if (txt) txt.textContent = bits.length ? bits.join(" · ") : "idle";
    } catch (_) { /* a meter must never break the page */ }
  }

  function startLoadMeter() {
    stopLoadMeter();
    pollServerLoad();
    loadTimer = setInterval(pollServerLoad, 3000);
  }

  function stopLoadMeter() {
    if (loadTimer) { clearInterval(loadTimer); loadTimer = null; }
  }

  let pdfProgressHigh = 0;
  let pdfStageHigh = 0;

  function resetPdfProgress() {
    pdfProgressHigh = 0;
    pdfStageHigh = 0;
  }

  function updatePdfProgress(stage, percent, title, copy) {
    const flow = $("pdfFlow");
    if (!flow) return;
    // Monotonic. The phases report their own scale and they overlap - the
    // notes phase opens at 8% after the transcript step has already reached
    // 24%, a warning asks for 45% after progress has passed it, and a status
    // sets 99.9% before the last progress events arrive. Each of those walked
    // the bar backwards, which reads as the job losing ground.
    const wanted = Math.max(0, Math.min(100, percent));
    pdfProgressHigh = Math.max(pdfProgressHigh, wanted);
    pdfStageHigh = Math.max(pdfStageHigh, stage);

    const bar = $("pdfFlowBar"), label = $("pdfFlowPercent");
    if (bar) bar.style.width = `${pdfProgressHigh}%`;
    if (label) label.textContent = `${Math.round(pdfProgressHigh)}%`;
    if ($("pdfFlowTitle")) $("pdfFlowTitle").textContent = title;
    if ($("pdfFlowCopy")) $("pdfFlowCopy").textContent = copy;
    for (const step of flow.querySelectorAll("[data-pdf-step]")) {
      const number = Number(step.dataset.pdfStep);
      step.classList.toggle("done", number < pdfStageHigh);
      step.classList.toggle("active", number === pdfStageHigh);
    }
  }

  function showPdfProgressError(message) {
    const flow = $("pdfFlow");
    if (!flow) return;
    flow.classList.add("error");
    updatePdfProgress(1, 0, "PDF generation couldn't start", message || "Please try again.");
  }

  function escapeAttr(s) {
    return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function status(text, spinning) {
    const el = $("rStatus");
    if (!el) return;
    el.innerHTML = text ? `${spinning ? '<span class="spin"></span>' : ""}<span>${escapeAttr(text)}</span>` : "";
  }

  function note(kind, html) {
    const el = $("rNote");
    if (el) el.innerHTML = html ? `<div class="note ${kind}">${html}</div>` : "";
  }

  function tools(items) {
    const bar = $("rTools");
    if (!bar) return;
    bar.innerHTML = "";
    items.forEach((it) => {
      const b = document.createElement("button");
      b.className = "tool " + (it.tone || "t-copy");
      b.innerHTML = it.icon + " " + it.label;
      b.onclick = it.onClick;
      bar.appendChild(b);
    });
  }

  const ICONS = {
    copy: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    pdf: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>',
    md: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 2h9l5 5v15H6z"/><path d="M14 2v6h6"/></svg>',
    notes: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 2h9l5 5v15H6z"/><path d="M14 2v6h6"/><path d="M9 13h6M9 17h4"/></svg>',
    open: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6"/><path d="M10 14L21 3"/></svg>',
  };

  // =====================================================================
  // The main flow
  // =====================================================================
  function setBusy(on) {
    busy = on;
    ["goBtn", "summaryBtn", "pdfBtn"].forEach((id) => { $(id).disabled = on; });
    $("goBtn").textContent = on ? "Working…" : "Summarize";
  }

  async function run(requestedMode, targetOverride, { pdfProgress = false } = {}) {
    if (busy) return;
    const url = $("url").value.trim();
    if (!url) { $("url").focus(); return; }

    if (!signedIn()) { openAuth("signup", "Create a free account to summarize — 5 videos free."); return; }

    const wantNotes = requestedMode === "notes";
    const target = targetOverride === undefined ? outLang() : targetOverride;
    videoChat = { context: null, history: [], busy: false };
    setBusy(true);
    if (pdfProgress) {
      resetPdfProgress();
      showPdfProgress(lastNotes);
      startLoadMeter();
    } else {
      shell(null);
      status("Reading the video…", true);
      note("", "");
    }

    // Show the thumbnail straight away — it costs nothing and makes the wait
    // feel much shorter.
    try {
      const info = await api("/video/info", { method: "POST", body: { url, device: device() } });
      const thumb = $("rThumb");
      if (thumb) {
        thumb.src = info.thumbnail;
        thumb.alt = info.title ? `${info.title} thumbnail` : "Video thumbnail";
        // The PDF progress card begins with an empty image while metadata is
        // loading. Reveal it explicitly once the real YouTube thumbnail URL
        // is available, even when a browser does not dispatch a second load.
        thumb.style.visibility = "visible";
      }
      $("rTitle").textContent = info.title;
      $("rMeta").innerHTML = info.author ? `<span class="tag">${escapeAttr(info.author)}</span>` : "";
      lastNotes = {
        videoId: info.video_id,
        title: info.title,
        author: info.author,
        thumbnail: info.thumbnail,
        url: info.url,
        markdown: "",
      };
      if (lastTranscript && lastTranscript.videoId !== info.video_id) lastTranscript = null;
    } catch (e) {
      if (e.status === 401) { setBusy(false); openAuth("login"); return; }
      if (e.status === 400) {
        setBusy(false);
        if (pdfProgress) showPdfProgressError(e.message); else { status(""); note("err", escapeAttr(e.message)); }
        return;
      }
    }

    try {
      if (wantNotes) await streamNotes(url, target);
      else await streamSummary(url, requestedMode, target);
    } catch (e) {
      if (pdfProgress) showPdfProgressError(e.message); else status("");
      if (e.status === 402) {
        const ent = e.entitlement;
        paintChip(ent);
        if (!pdfProgress) note("warn",
          `<b>${escapeAttr(e.message)}</b><br>` +
          `Your free videos are used up. <button class="linkbtn" onclick="document.getElementById('accountBtn').click()">Subscribe for $5/month →</button>`);
      } else if (e.status === 401) {
        openAuth("login");
      } else if (e.status === 422 && !pdfProgress) {
        note("err", escapeAttr(e.message));
      } else if (!pdfProgress) {
        note("err", escapeAttr(e.message || "Something went wrong."));
      }
    } finally {
      setBusy(false);
      refreshEntitlement();
    }
  }

  /** Read an NDJSON stream, calling onEvent for each line as it arrives. */
  // --- Extension bridge ----------------------------------------------------
  // When the TubeNotes extension is installed it can read the transcript from
  // this user's own browser, so YouTube sees a person rather than our server.
  // Free, unlimited, never rate-limited. With no extension we send nothing and
  // the server fetches it the old way - so nothing here can break a visitor
  // who does not have it.
  let extReady = false;
  const extWaiters = new Map();

  window.addEventListener("message", (ev) => {
    if (ev.source !== window) return;
    const d = ev.data;
    if (!d || d.source !== "tubenotes-ext") return;
    if (d.type === "READY") { extReady = true; return; }
    if (d.type === "TRANSCRIPT") {
      const done = extWaiters.get(d.reqId);
      if (done) { extWaiters.delete(d.reqId); done(d); }
    }
  });

  // The content script may have loaded before this file; a ping makes sure we
  // hear its READY either way.
  try { window.postMessage({ source: "tubenotes-page", type: "PING" }, window.location.origin); } catch (_) {}

  function extAsk(videoId, ms) {
    return new Promise((resolve) => {
      const reqId = "r" + Math.random().toString(36).slice(2);
      const timer = setTimeout(() => { extWaiters.delete(reqId); resolve(null); }, ms);
      extWaiters.set(reqId, (d) => { clearTimeout(timer); resolve(d && d.ok ? d : null); });
      window.postMessage(
        { source: "tubenotes-page", type: "GET_TRANSCRIPT", videoId, reqId },
        window.location.origin
      );
    });
  }

  function videoIdFrom(input) {
    const v = (input || "").trim();
    if (/^[A-Za-z0-9_-]{11}$/.test(v)) return v;
    try {
      const u = new URL(v.includes("://") ? v : "https://" + v);
      const host = u.hostname.replace(/^www\.|^m\./, "");
      if (host === "youtu.be") return u.pathname.slice(1).split("/")[0] || null;
      if (u.pathname === "/watch") return u.searchParams.get("v");
      const parts = u.pathname.split("/").filter(Boolean);
      if (parts.length >= 2 && ["shorts", "embed", "live", "v"].includes(parts[0])) return parts[1];
    } catch (_) {}
    return null;
  }

  async function extraFromExtension(url) {
    const videoId = videoIdFrom(url);
    if (lastTranscript && lastTranscript.videoId === videoId && lastTranscript.text) {
      return {
        transcript: lastTranscript.text,
        transcript_lang: lastTranscript.lang || null,
      };
    }

    // BAND. Ye pul extension se transcript maangta tha aur 30 second tak uska
    // intezaar karta tha. Wo raasta kabhi kaam nahi kiya: YouTube ab
    // api/timedtext par HTTP 200 ke saath KHAALI body lautata hai - server se,
    // extension ke service worker se, aur khud YouTube ke page ke andar se bhi.
    //
    // Jab tak extension lagi nahi thi, extReady false rehta tha aur ye chupchap
    // lautta tha. Extension lagte hi wo READY bhejne lagi aur har summary se 21
    // second cheen liye. Naapa gaya, ek hi video par:
    //     extension lagi hui  ->  Queued at 21,300 ms
    //     incognito (band)    ->  Queued at    293 ms
    //
    // Backend ka `transcript` field waise hi hai - wo nuksaan nahi karta aur
    // kabhi mobile app banane par wahi raasta kaam aayega.
    return {};
  }

  async function streamNdjson(path, body, onEvent) {
    const res = await api(path, { method: "POST", body, raw: true });
    if (!res.ok) {
      const text = await res.text();
      let json = null; try { json = JSON.parse(text); } catch (_) {}
      if (res.status === 401) tokens.clear();
      throw err(res.status, json ? json.detail : text);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        try {
          onEvent(JSON.parse(line));
        } catch (err) {
          // A malformed line is worth ignoring; a bug in the handler is not.
          // Swallowing both is how two ReferenceErrors hid behind a progress
          // bar that simply never moved.
          if (!(err instanceof SyntaxError)) console.error("event handler failed", err);
        }
      }
    }
  }

  async function streamSummary(url, m, target) {
    let text = "";
    let failed = null;
    let langOut = null;
    const body = $("rBody");
    // Same mistake as above: this was read from streamNotes's scope, so the
    // summary phase threw on its first delta and left the bar at 24%.
    const inPdfFlow = Boolean($("pdfFlow"));

    const extra = await extraFromExtension(url);
    await streamNdjson("/summarize", { url, device: device(), mode: m, target_lang: target || null, ...extra }, (ev) => {
      if (ev.type === "meta") {
        paintChip(ev.entitlement);
        langOut = ev.language;
        if (ev.transcript) {
          lastTranscript = {
            videoId: ev.video.video_id,
            text: ev.transcript,
            lang: ev.transcript_lang || ev.detected_language || null,
          };
        }
        lastNotes = {
          videoId: ev.video.video_id, title: ev.video.title, url: ev.video.url,
          markdown: "", lang: ev.language, jobId: ev.job_id || null,
        };
        const translated = ev.detected_language && ev.detected_language !== ev.language;
        $("rMeta").innerHTML =
          (ev.video.author ? `<span class="tag">${escapeAttr(ev.video.author)}</span>` : "") +
          `<span class="tag" id="rLangTag">🌐 ${escapeAttr(ev.language_name)}</span>` +
          (translated ? `<span class="tag">video: ${escapeAttr(ev.detected_language_name)}</span>` : "") +
          `<span class="tag">${(ev.transcript_chars / 1000).toFixed(1)}k chars</span>`;
        status("Writing the summary…", true);
      } else if (ev.type === "delta") {
        text += ev.text;
        // The PDF card has no rBody - it is a different view entirely - so this
        // must not assume the element is there. It threw on the first delta of
        // every PDF run, which is why the summary phase never reported.
        if (body) body.innerHTML = md2html(text) + '<span class="cursor"></span>';
        if (inPdfFlow) {
          // The PDF flow runs /summarize before /notes, and progress events
          // only exist for notes - so this whole phase used to leave the bar
          // parked on the 24% its meta event set, for as long as the summary
          // took. Advance it with the text actually arriving instead.
          const share = Math.min(1, text.length / 3000);
          updatePdfProgress(
            2, 10 + share * 14, "Summarising…",
            "Reading the video and writing the overview. The detailed notes start next."
          );
        }
      } else if (ev.type === "status") {
        status(ev.message, true);
      } else if (ev.type === "done") {
        text = ev.text || text;
        if (body) body.innerHTML = md2html(text);
        if (ev.language) langOut = ev.language;
        if (ev.partial) note("warn", "The model stopped early — this is what it produced.");
      } else if (ev.type === "error") {
        failed = ev.message;
      }
    });

    status("");
    if (failed) { note("err", escapeAttr(failed)); return; }
    if (lastNotes) { lastNotes.markdown = text; lastNotes.lang = langOut; }
    finishTools(text);
  }

  async function streamNotes(url, target) {
    let text = "";
    let failed = null;
    let langOut = null;
    let partsDone = 0;
    let metaVideo = null;
    const warnings = [];
    // Declared HERE, in the function that uses them. They lived in run()
    // before, which does not enclose this one, so every progress and part
    // event threw ReferenceError - silently, see streamNdjson - and the bar
    // never moved at all.
    const liveParts = [];
    let notesStartedAt = 0;
    const inPdfFlow = Boolean($("pdfFlow"));
    if (inPdfFlow) {
      updatePdfProgress(1, 8, "Reading video…", "Fetching the transcript for your complete PDF notes.");
    } else {
      $("rProgress").classList.remove("hidden");
      status("Reading the whole video…", true);
    }

    const extra = await extraFromExtension(url);
    await streamNdjson("/notes", { url, device: device(), target_lang: target || null, ...extra }, (ev) => {
      if (ev.type === "meta") {
        metaVideo = ev.video;
        langOut = ev.language;
        const translated = ev.detected_language && ev.detected_language !== ev.language;
        $("rMeta").innerHTML =
          (ev.video.author ? `<span class="tag">${escapeAttr(ev.video.author)}</span>` : "") +
          `<span class="tag" id="rLangTag">🌐 ${escapeAttr(ev.language_name)}</span>` +
          (translated ? `<span class="tag">video: ${escapeAttr(ev.detected_language_name)}</span>` : "");
        lastNotes = {
          videoId: ev.video.video_id, title: ev.video.title, url: ev.video.url,
          markdown: "", lang: ev.language, jobId: ev.job_id || null,
        };
        if (inPdfFlow) {
          updatePdfProgress(2, 24, "Analyzing content…", "Transcript is ready. Planning the detailed notes in the video’s language.");
        }
      } else if (ev.type === "status") {
        if (inPdfFlow) {
          updatePdfProgress(4, 99.9, "Preparing PDF…", ev.message);
        } else {
          status(ev.message, true);
        }
      } else if (ev.type === "warning") {
        warnings.push(ev.message);
        if (inPdfFlow) {
          updatePdfProgress(3, 45, "Writing notes…", "Some sections need attention; the available notes are still being prepared.");
        } else {
          note("warn", warnings.map(escapeAttr).join("<br>"));
        }
      } else if (ev.type === "part") {
        // Live notes. Parts are written in parallel and arrive interleaved, so
        // each is buffered by its own index and the whole document is
        // reassembled in order on every update - watching the text appear beats
        // watching a percentage.
        if (!liveParts[ev.index]) liveParts[ev.index] = "";
        liveParts[ev.index] += ev.text || "";
        const joined = liveParts.filter((p) => p != null).join("\n\n");
        const target = inPdfFlow ? $("pdfFlowLive") : $("rBody");
        if (target) {
          target.hidden = false;
          target.innerHTML = md2html(joined);
          // Follow the text as it is written, without moving the page.
          target.scrollTop = target.scrollHeight;
        }
      } else if (ev.type === "ping") {
        // Keepalive only - the server is still working. It exists so the
        // connection is never silent long enough for Cloudflare to close it.
      } else if (ev.type === "progress") {
        partsDone = ev.total;
        // Parts are written in parallel, so "3 of 12 done, 9 being written"
        // is the honest description. Saying only "part 3 of 12" made a busy
        // server look like a stalled one.
        const inFlight = Math.max(0, (ev.started || 0) - ev.done);
        // Nothing started yet means the server is full and this job is waiting
        // its turn. Saying so is the honest thing: a bar parked on one number
        // reads as broken, and people close the tab on it.
        const queued = ev.done === 0 && inFlight === 0;
        if (!notesStartedAt) notesStartedAt = Date.now();
        const waited = Math.round((Date.now() - notesStartedAt) / 1000);
        const detail = queued
          ? `waiting for a free slot on the server — ${ev.total} parts queued (${waited}s)`
          : ev.done > 0
            ? `${ev.done} of ${ev.total} parts written` + (inFlight ? `, ${inFlight} in progress` : "")
            : `${ev.total} parts, ${inFlight} being written now`;
        if (inPdfFlow) {
          // Ceiling 99.9, not 90: the last stretch of a long job is real
          // progress and hiding it made a nearly-finished PDF look stuck.
          const percent = queued ? 26 : Math.max(26, Math.min(99.9, ev.percent));
          updatePdfProgress(
            3, percent,
            queued ? "Queued — server is busy…" : "Writing detailed notes…",
            queued ? `${detail}. Your place is held; this will start automatically.`
                   : `${detail}. Every section is included in your PDF.`
          );
        } else {
          $("rBar").style.width = Math.max(2, ev.percent) + "%";
          status((queued ? "Queued — " : "Writing detailed notes — ") + detail, true);
        }
      } else if (ev.type === "done") {
        text = ev.text || "";
        if (!inPdfFlow) $("rBody").innerHTML = md2html(text);
      } else if (ev.type === "error") {
        failed = ev.message;
      }
    });

    if (!inPdfFlow) $("rProgress").classList.add("hidden");
    if (failed) {
      if (inPdfFlow) showPdfProgressError(failed); else { status(""); note("err", escapeAttr(failed)); }
      return;
    }

    // Let the final PDF stage render before changing back to the normal notes
    // card and opening the print dialog.
    if (inPdfFlow) {
      updatePdfProgress(4, 100, "Preparing PDF…", "Your detailed notes are ready. Opening the print dialog.");
      await new Promise((resolve) => setTimeout(resolve, 350));
      shell(metaVideo);
      const translated = metaVideo && langOut && metaVideo.detected_language && metaVideo.detected_language !== langOut;
      $("rMeta").innerHTML =
        (metaVideo && metaVideo.author ? `<span class="tag">${escapeAttr(metaVideo.author)}</span>` : "") +
        `<span class="tag" id="rLangTag">🌐 ${escapeAttr(langOut || "")}</span>` +
        (translated ? `<span class="tag">video: ${escapeAttr(metaVideo.detected_language)}</span>` : "");
      $("rBody").innerHTML = md2html(text);
    }
    if (lastNotes) { lastNotes.markdown = text; lastNotes.lang = langOut; }

    status(`All ${partsDone || "?"} parts written — building the PDF…`, true);
    // The live pane exists to show work happening. The work has happened, and
    // leaving the streamed text sitting in the progress card makes the finished
    // PDF look like it is still being written.
    const livePane = $("pdfFlowLive");
    if (livePane) { livePane.innerHTML = ""; livePane.hidden = true; }
    stopLoadMeter();
    finishTools(text, true);
    // Only now, with every part written, do we open the print dialog.
    openPrintView();
    status("");
  }

  /** Convert the summary that is already on screen into another language. */
  async function translateTo(code) {
    if (!lastNotes || !lastNotes.markdown || !code) return false;
    const sel = $("rLang");
    if (sel) sel.disabled = true;
    status("Translating…", true);
    note("", "");
    try {
      const res = await api("/translate", {
        method: "POST",
        body: { text: lastNotes.markdown, target_lang: code },
      });
      lastNotes.markdown = res.text;
      lastNotes.lang = res.target_lang;
      $("rBody").innerHTML = md2html(res.text);
      resetVideoChatContext(res.text);
      const tag = $("rLangTag");
      if (tag) tag.textContent = `🌐 ${res.language_name}`;
      status("");
      // The PDF is built from lastNotes.markdown, so it now follows too.
      return true;
    } catch (e) {
      status("");
      note("err", e.status === 503
        ? "Translation service is unreachable right now."
        : escapeAttr(e.message || "Translation failed."));
      return false;
    } finally {
      if (sel) { sel.disabled = false; sel.value = ""; }
    }
  }

  function finishTools(text, isNotes) {
    tools([
      { label: "Copy Summary", icon: ICONS.copy, tone: "t-copy", onClick: () => {
          navigator.clipboard.writeText(text).then(() => status("Copied to clipboard"));
        } },
      ...(isNotes ? [] : [{ label: "Full Notes PDF", icon: ICONS.notes, tone: "t-notes",
                            onClick: () => pdfFlow({ full: true }) }]),
      { label: "Open on YouTube", icon: ICONS.open, tone: "t-youtube", onClick: () => {
          if (lastNotes) window.open(lastNotes.url, "_blank", "noopener");
        } },
    ]);

    // Translate picker — applies to what is on screen AND to the PDF, since the
    // PDF is rendered from the same markdown.
    const sel = document.createElement("select");
    sel.className = "tool t-lang";
    sel.id = "rLang";
    sel.title = "Translate this summary (the PDF follows too)";
    fillLangSelect(sel, { includeAuto: false });
    sel.addEventListener("change", () => translateTo(sel.value));
    $("rTools").appendChild(sel);
    if (!isNotes) mountVideoChat(text);
  }

  function chatStarter() {
    const copy = CHAT_STARTER_COPY[lastNotes?.lang] || CHAT_STARTER_COPY.en;
    return `
      <div class="chat-starter" id="chatStarter">
        <p>${copy[0]}</p>
        <div class="chat-suggestions" aria-label="Suggested questions">
          <button type="button" data-chat-question="${copy[1]}">${copy[1]}</button>
          <button type="button" data-chat-question="${copy[2]}">${copy[2]}</button>
          <button type="button" data-chat-question="${copy[3]}">${copy[3]}</button>
        </div>
      </div>`;
  }

  function bindChatSuggestions() {
    document.querySelectorAll("[data-chat-question]").forEach((button) => {
      button.addEventListener("click", () => {
        const input = $("videoChatInput");
        const form = $("videoChatForm");
        if (!input || !form || videoChat.busy) return;
        input.value = button.dataset.chatQuestion || "";
        form.requestSubmit();
      });
    });
  }

  function chatCard() {
    const copy = CHAT_STARTER_COPY[lastNotes?.lang] || CHAT_STARTER_COPY.en;
    return `
      <section class="video-chat" id="videoChat" aria-label="Ask questions about this video">
        ${chatStarter()}
        <div class="chat-messages" id="chatMessages" aria-live="polite"></div>
        <form class="chat-form" id="videoChatForm">
          <textarea id="videoChatInput" rows="1" maxlength="2000" placeholder="${copy[4]}" aria-label="${copy[4]}" required></textarea>
          <button class="chat-send" id="videoChatSend" type="submit" aria-label="Send question" title="Send question">
            <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></svg>
          </button>
        </form>
      </section>`;
  }

  function mountVideoChat(summary) {
    if (!summary || !lastNotes) return;
    const old = $("videoChat");
    if (old) old.remove();
    videoChat = {
      context: { summary, language: lastNotes.lang || "en" }, history: [], busy: false,
    };
    // Keep follow-up chat within the same result card as the summary, rather
    // than creating a visually separate card beneath it.
    const summaryBody = $("rBody");
    if (!summaryBody) return;
    summaryBody.insertAdjacentHTML("afterend", chatCard());
    const form = $("videoChatForm");
    const input = $("videoChatInput");
    form.addEventListener("submit", sendVideoChat);
    bindChatSuggestions();
    // Chat convention: Enter sends. Shift + Enter is reserved for a new line.
    // isComposing protects Hindi/Indic IME users while they are choosing text.
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
      event.preventDefault();
      form.requestSubmit();
    });
  }

  function resetVideoChatContext(summary) {
    if (!videoChat.context || !lastNotes || !summary) return;
    videoChat.context = { summary, language: lastNotes.lang || "en" };
    videoChat.history = [];
    const messages = $("chatMessages");
    if (!messages) return;
    messages.innerHTML = "";
    $("chatStarter")?.remove();
    messages.insertAdjacentHTML("beforebegin", chatStarter());
    bindChatSuggestions();
  }

  function chatTranslationTarget(question) {
    const q = String(question || "").toLocaleLowerCase();
    const asksToTranslate = /translate|translation|convert|conversion|अनुवाद|ट्रांसलेट|कन्वर्ट|भाषा.*(?:बदल|कर)|language.*(?:change|switch)|\b(?:me|mein)\s+(?:kar|karo|bana|convert)/i.test(q);
    if (!asksToTranslate) return null;
    for (const [code, aliases] of CHAT_LANGUAGE_ALIASES) {
      if (aliases.some((alias) => q.includes(alias.toLocaleLowerCase()))) return code;
    }
    return null;
  }

  function chatReplyLanguage(question) {
    // "Hindi me answer do" and "explain in French" should change only the
    // reply language. A translation command is handled separately above.
    const q = String(question || "").toLocaleLowerCase();
    const asksForReply = /answer|reply|respond|explain|describe|tell|bata|samjha|bol|language|\bin\b|\bme\b|\bmein\b/i.test(q);
    if (!asksForReply) return null;
    for (const [code, aliases] of CHAT_LANGUAGE_ALIASES) {
      if (aliases.some((alias) => q.includes(alias.toLocaleLowerCase()))) return code;
    }
    return null;
  }

  function chatPdfAction(question) {
    const q = String(question || "").toLocaleLowerCase();
    if (!/(\bpdf\b|पीडीएफ)/i.test(q)) return null;
    if (/(full\s*(notes?|pdf)|notes?.*pdf|detailed.*pdf|पूरा.*पीडीएफ|फुल.*नोट्स)/i.test(q)) return "full";
    if (/(download|डाउनलोड|save|चाहिए|chahiye|get|लेना)/i.test(q)) return "download";
    return "options";
  }

  function showPdfButtons(action) {
    const toolbar = $("rTools");
    if (!toolbar) return;
    const targets = Array.from(toolbar.querySelectorAll(".t-notes"));
    toolbar.scrollIntoView({ behavior: "smooth", block: "center" });
    targets.forEach((button) => {
      button.classList.remove("chat-pdf-focus");
      // Restart the animation even when a visitor asks twice in a row.
      void button.offsetWidth;
      button.classList.add("chat-pdf-focus");
      setTimeout(() => button.classList.remove("chat-pdf-focus"), 3000);
    });
  }

  async function translateSummaryFromChat(target) {
    if (!videoChat.context || !videoChat.context.summary) {
      throw err(422, "Generate a summary before translating it.");
    }
    const res = await api("/video-chat/translate-summary", {
      method: "POST",
      // Keep the original on-screen summary unchanged. Every command starts
      // from that original context, so English, Tamil and Gujarati versions
      // can appear together in the chat without translating a translation.
      body: { summary: videoChat.context.summary, target_lang: target },
    });
    return res;
  }

  async function copyChatReply(button, text) {
    let copied = false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        copied = true;
      }
    } catch (_) {
      // Older browsers can use the short-lived textarea fallback below.
    }
    if (!copied) {
      const helper = document.createElement("textarea");
      helper.value = text;
      helper.setAttribute("readonly", "");
      helper.style.cssText = "position:fixed;opacity:0;pointer-events:none";
      document.body.appendChild(helper);
      try {
        helper.select();
        copied = document.execCommand("copy");
      } finally {
        helper.remove();
      }
    }
    if (!copied) return;
    button.classList.add("copied");
    button.setAttribute("aria-label", "Reply copied");
    button.title = "Copied";
    setTimeout(() => {
      button.classList.remove("copied");
      button.setAttribute("aria-label", "Copy reply");
      button.title = "Copy reply";
    }, 1800);
  }

  function addChatMessage(role, text, typing) {
    const messages = $("chatMessages");
    if (!messages) return null;
    if (role === "user") $("chatStarter")?.remove();
    const empty = messages.querySelector(".chat-empty");
    if (empty) empty.remove();
    const item = document.createElement("article");
    item.className = `chat-message ${role}`;
    item.innerHTML = typing
      ? '<span class="chat-typing"><i></i>Thinking about the video…</span>'
      : `<span class="chat-label ${role === "user" ? "user-label" : "assistant-label"}">${role === "user" ? "You" : "TubeNotes"}</span><div class="chat-copy">${md2html(text)}</div>`;
    if (!typing && role === "assistant") {
      item.classList.add("has-copy");
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "chat-reply-copy";
      copy.setAttribute("aria-label", "Copy reply");
      copy.title = "Copy reply";
      copy.innerHTML = `${ICONS.copy}<svg class="chat-reply-copied" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>`;
      copy.addEventListener("click", () => {
        copyChatReply(copy, item.querySelector(".chat-copy")?.innerText || text);
      });
      item.appendChild(copy);
    }
    messages.appendChild(item);
    messages.scrollTop = messages.scrollHeight;
    return item;
  }

  async function sendVideoChat(event) {
    event.preventDefault();
    if (videoChat.busy || !videoChat.context) return;
    const input = $("videoChatInput");
    const send = $("videoChatSend");
    const question = input.value.trim();
    if (!question) return;
    const prior = videoChat.history.slice(-10);
    const translateTarget = chatTranslationTarget(question);
    const replyLanguage = chatReplyLanguage(question);
    const pdfAction = chatPdfAction(question);
    videoChat.busy = true;
    input.value = "";
    input.disabled = true;
    send.disabled = true;
    addChatMessage("user", question);
    const pending = addChatMessage("assistant", "", true);
    try {
      if (pdfAction) {
        if (pending) pending.remove();
        showPdfButtons(pdfAction);
        addChatMessage("assistant", "Please use the **Full Notes PDF** button above to generate and download the detailed PDF notes.");
        return;
      }
      if (translateTarget) {
        const res = await translateSummaryFromChat(translateTarget);
        if (pending) pending.remove();
        addChatMessage("assistant", `## ${res.language_name} translation\n\n${res.text}`);
        return;
      }
      const res = await api("/video-chat", {
        method: "POST",
        body: {
          summary: videoChat.context.summary,
          language: videoChat.context.language,
          reply_language: replyLanguage,
          question,
          history: prior,
        },
      });
      if (pending) pending.remove();
      addChatMessage("assistant", res.answer || "I could not find an answer in this video’s summary.");
      videoChat.history.push({ role: "user", content: question }, { role: "assistant", content: res.answer || "" });
      videoChat.history = videoChat.history.slice(-12);
    } catch (e) {
      if (pending) pending.remove();
      addChatMessage("assistant", e.status === 401
        ? "Please sign in again to continue this chat."
        : (e.message || "Video chat is temporarily unavailable. Please try again."));
    } finally {
      videoChat.busy = false;
      if ($("videoChatInput")) $("videoChatInput").disabled = false;
      if ($("videoChatSend")) $("videoChatSend").disabled = false;
      if ($("videoChatInput")) $("videoChatInput").focus();
    }
  }

  function downloadMd(text) {
    if (!lastNotes) return;
    text = text || lastNotes.markdown;
    const md = `# ${lastNotes.title}\n\nSource: ${lastNotes.url}\n\n---\n\n${text}`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([md], { type: "text/markdown;charset=utf-8" }));
    a.download = (lastNotes.title.replace(/[\\/:*?"<>|]/g, "").trim().slice(0, 90) || "summary") + ".md";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }

  /**
   * PDF = a print-ready page + the browser's "Save as PDF".
   * Deliberate: server-side PDF engines need Devanagari/Tamil font packaging to
   * avoid tofu boxes, while the browser already has those fonts and renders the
   * page perfectly.
   */
  function buildPrintHtml(standalone) {
    if (!lastNotes || !lastNotes.markdown) return null;
    let content = lastNotes.markdown;
    let heading = lastNotes.title;
    const m = content.match(/^\s*#\s+(.+)\s*(?:\n|$)/);
    if (m) { heading = m[1].trim(); content = content.slice(m[0].length); }

    return `<!doctype html><html lang="${escapeAttr(lastNotes.lang || "en")}"><head><meta charset="utf-8">
<title>${escapeAttr(heading)}</title>
<style>
:root{--ink:#1a1a1a;--muted:#6b7280;--line:#e5e7eb;
--c1:#4f46e5;--c1bg:#eef0ff;--c2:#0f9d58;--c2bg:#e7f6ee;--c3:#d97706;--c3bg:#fdf1de;
--c4:#db2777;--c4bg:#fce8f1;--c5:#0284c7;--c5bg:#e2f2fb}
*{box-sizing:border-box}
body{font-family:"Segoe UI","Noto Sans","Noto Sans Devanagari","Noto Sans Tamil","Nirmala UI",Arial,sans-serif;
max-width:780px;margin:0 auto 60px;padding:0 26px;color:var(--ink);line-height:1.85;font-size:16px}
.cover{margin:0 -26px 28px;padding:32px 34px 24px;
background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 50%,#db2777 100%);color:#fff;border-radius:0 0 20px 20px}
.cover .kicker{font-size:11px;letter-spacing:.14em;text-transform:uppercase;opacity:.9;font-weight:700;margin-bottom:10px}
.cover h1{font-size:25px;line-height:1.3;margin:0 0 14px;font-weight:800}
.cover .src{font-size:12px;word-break:break-all;background:rgba(255,255,255,.16);padding:7px 12px;border-radius:8px;display:inline-block;max-width:100%}
.cover .src a{color:#fff;text-decoration:none}
h2{font-size:19px;font-weight:800;margin:30px 0 12px;padding:11px 16px;border-radius:10px;
border-left:6px solid var(--c1);background:var(--c1bg)}
h2:nth-of-type(5n+2){border-left-color:var(--c2);background:var(--c2bg)}
h2:nth-of-type(5n+3){border-left-color:var(--c3);background:var(--c3bg)}
h2:nth-of-type(5n+4){border-left-color:var(--c4);background:var(--c4bg)}
h2:nth-of-type(5n+5){border-left-color:var(--c5);background:var(--c5bg)}
h3{font-size:16px;font-weight:700;margin:18px 0 6px;color:#374151}
p{margin:9px 0;text-align:justify}strong{color:#000;font-weight:700}
ul,ol{margin:8px 0 12px;padding-left:4px}
li{margin:6px 0;list-style:none;padding-left:24px;position:relative}
ul li::before{content:"";position:absolute;left:6px;top:10px;width:7px;height:7px;border-radius:50%;background:#4f46e5}
ol{counter-reset:item}ol li{counter-increment:item}
ol li::before{content:counter(item);position:absolute;left:0;top:2px;width:19px;height:19px;border-radius:50%;
background:#4f46e5;color:#fff;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center}
.footer{margin-top:40px;padding-top:14px;border-top:2px solid var(--line);font-size:11px;color:var(--muted);text-align:center}
.hint{background:#fffbeb;border:1px solid #fcd34d;padding:11px 15px;border-radius:10px;font-size:13px;margin:18px 0 4px;color:#92400e}
@media print{.hint{display:none}body{margin:0;max-width:100%}.cover{border-radius:0}
*{-webkit-print-color-adjust:exact!important;print-color-adjust:exact!important}}
</style></head><body>
${standalone ? '<div class="hint">In the print dialog choose <b>Destination: Save as PDF</b>, then <b>Save</b>. Keep <b>More settings \u2192 Background graphics</b> ON so the colours print.</div>' : ""}
<div class="cover"><div class="kicker">\u2728 VIDEO NOTES</div>
<h1>${escapeAttr(heading)}</h1>
<div class="src">\ud83d\udd17 <a href="${escapeAttr(lastNotes.url)}">${escapeAttr(lastNotes.url)}</a></div></div>
${md2html(content)}
<div class="footer">Generated by TubeNotes</div>
${standalone ? '<scr' + 'ipt>setTimeout(function(){window.print()},450)</scr' + 'ipt>' : ""}
</body></html>`;
  }

  /**
   * Show the print dialog WITHOUT opening a popup window.
   *
   * window.open() is only permitted while a user gesture is still "live".
   * Generating full notes takes minutes, so by the time the document is ready
   * the gesture has long expired and Chrome blocks the window - which is the
   * "browser blocked the popup" message people were hitting. Printing from a
   * hidden same-origin iframe needs no popup permission at all, so it works
   * however long the generation took.
   */
  function openPrintView() {
    const html = buildPrintHtml(false);
    if (!html) return;

    // The browser, not the server, renders this colourful document. Record
    // that the authenticated user reached the PDF-ready stage so Admin sees
    // the same PDF icon that the user sees in this flow.
    if (lastNotes && lastNotes.jobId) {
      api("/jobs/pdf-ready", { method: "POST", body: { job_id: lastNotes.jobId } }).catch(() => {});
    }

    const old = document.getElementById("tnPrintFrame");
    if (old) old.remove();

    const frame = document.createElement("iframe");
    frame.id = "tnPrintFrame";
    frame.setAttribute("aria-hidden", "true");
    frame.style.cssText =
      "position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden";
    frame.srcdoc = html;                       // same-origin, so we may print it
    frame.onload = () => {
      // A beat for fonts (Devanagari, Tamil…) to lay out before the dialog.
      setTimeout(() => {
        try {
          frame.contentWindow.focus();
          frame.contentWindow.print();
          showPrintReady(false);
        } catch (e) {
          showPrintReady(true);
        }
      }, 350);
    };
    document.body.appendChild(frame);
  }

  /**
   * Shown once the document is fully built and the print dialog has been
   * triggered. The button is always present rather than only on failure -
   * there is no reliable way to detect that the dialog actually opened, and a
   * visible "Save as PDF" is more useful than a question about whether it did.
   */
  function showPrintReady(failed) {
    // Its own slot: a missing-section warning from the notes run must stay
    // visible next to this, not be overwritten by it.
    const el = $("rPrint");
    if (!el) return;
    const msg = failed
      ? "The document is ready, but the print dialog didn't open by itself."
      : "Ready — choose <b>Destination: Save as PDF</b> in the print dialog.";
    el.innerHTML =
      `<div class="note ${failed ? "warn" : "ok"}">` +
      `<b>${failed ? "\u26a0" : "\u2705"}</b> ${msg} ` +
      `<button class="linkbtn" id="tnPrintAgain">Open print view</button></div>`;
    const btn = $("tnPrintAgain");
    if (btn) btn.onclick = openPrintTab;      // a real click, so this is allowed
  }

  /** Opens the document in a normal tab. Safe: called straight from a click. */
  function openPrintTab() {
    const html = buildPrintHtml(true);   // with the hint + auto print dialog
    if (!html) return;
    const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
    window.open(url, "_blank", "noopener");
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }


  // =====================================================================
  // Language picker — shown when a PDF button is clicked, so the user
  // chooses the language of the document they are about to get.
  // =====================================================================
  const LANG_BY_CODE = Object.fromEntries(LANGS);

  function pickLanguage({ title, lead, firstOption }) {
    return new Promise((resolve) => {
      const overlay = $("langPicker");
      const list = $("pickList");
      const search = $("pickSearch");
      $("pickTitle").textContent = title;
      $("pickLead").textContent = lead;
      search.value = "";

      const close = (value) => {
        overlay.classList.add("hidden");
        overlay.removeEventListener("click", onBackdrop);
        document.removeEventListener("keydown", onKey);
        resolve(value);
      };
      const onBackdrop = (e) => { if (e.target === overlay) close(null); };
      const onKey = (e) => { if (e.key === "Escape") close(null); };

      function render(filter) {
        const q = (filter || "").trim().toLowerCase();
        list.innerHTML = "";

        if (firstOption && !q) {
          const b = document.createElement("button");
          b.className = "pick-item suggested";
          b.innerHTML = `<span>${escapeAttr(firstOption.label)}</span>`;
          b.onclick = () => close(firstOption.value);
          list.appendChild(b);
        }

        const rows = LANGS.filter(([code, name]) =>
          !q || name.toLowerCase().includes(q) || code.includes(q)
        );
        if (!rows.length) {
          list.innerHTML = '<div class="pick-empty">No language matches that.</div>';
          return;
        }
        for (const [code, name] of rows) {
          const b = document.createElement("button");
          b.className = "pick-item";
          b.innerHTML = `<span>${escapeAttr(name)}</span><span class="code">${code}</span>`;
          b.onclick = () => close(code);
          list.appendChild(b);
        }
      }

      search.oninput = () => render(search.value);
      $("pickClose").onclick = () => close(null);
      overlay.addEventListener("click", onBackdrop);
      document.addEventListener("keydown", onKey);

      render("");
      overlay.classList.remove("hidden");
      setTimeout(() => search.focus(), 40);
    });
  }

  /** Build the PDF the user asked for, in the language they just picked. */
  async function pdfFlow({ full }) {
    const url = $("url").value.trim();
    const haveCurrent =
      lastNotes && lastNotes.markdown && url.includes(lastNotes.videoId);

    // Full Notes and the home-page PDF button need detailed notes first.
    // A current summary can be printed immediately after an optional
    // translation, but it still uses the exact same language picker.
    const willGenerate = full || !haveCurrent;

    const firstOption = willGenerate
      ? { value: "", label: "Same as the video language" }
      : { value: "", label: `Keep current — ${LANG_BY_CODE[lastNotes.lang] || lastNotes.lang || "as shown"}` };

    const choice = await pickLanguage({
      title: full ? "Full Notes PDF language" : "PDF language",
      lead: willGenerate
        ? "Full Notes will be created in this language, then opened as a PDF."
        : "The PDF will be written in this language.",
      firstOption,
    });
    if (choice === null) return;          // cancelled

    // The selected target is passed to /notes. Gemma writes supported target
    // languages directly; for other supported languages the backend uses its
    // Gemma translation flow before the browser creates the PDF.
    if (willGenerate) {
      await run("notes", choice || null, { pdfProgress: true });
      return;
    }

    // A summary is already on screen: keep it, translating only if needed.
    if (choice && choice !== "auto" && choice !== lastNotes.lang) {
      if (!await translateTo(choice)) return;
    }
    openPrintView();
  }

  // =====================================================================
  // Auth UI
  // =====================================================================
  let authMode = "login";
  let accountUser = null;
  let currentBillingPlan = null;

  function priceForPlan(plan) {
    if (!plan) return null;
    return plan.currency === "INR"
      ? { label: "₹299/month", detail: "₹299/month" }
      : { label: "$5/month", detail: "$5/month" };
  }

  function setHeroBillingPrice(plan) {
    const price = priceForPlan(plan);
    if (price && mode === "summary") {
      $("heroHint").textContent = `5 free videos on signup · then ${price.label} · works with Hindi, English & 40+ languages`;
    }
  }

  function paintBillingPrice(pro) {
    const price = priceForPlan(currentBillingPlan);
    $("upgradeBtn").textContent = pro
      ? "Subscription active"
      : price
        ? `Subscribe — ${price.label}`
        : "Subscribe";
    setHeroBillingPrice(currentBillingPlan);
  }

  async function loadBillingPrice() {
    currentBillingPlan = await api("/billing/price", { auth: false, cache: "no-store" });
    setHeroBillingPrice(currentBillingPlan);
    return currentBillingPlan;
  }

  function openAuth(which, lead) {
    $("authModal").classList.remove("hidden");
    $("authPane").classList.remove("hidden");
    $("acctPane").classList.add("hidden");
    setAuthMode(which === "signup" ? "signup" : "login");
    if (lead) $("authLead").textContent = lead;
  }

  function setAuthMode(m) {
    authMode = m;
    const up = m === "signup";
    $("tabIn").classList.toggle("on", !up);
    $("tabUp").classList.toggle("on", up);
    $("nameWrap").classList.toggle("hidden", !up);
    $("authTitle").textContent = up ? "Create your account" : "Welcome back";
    $("authLead").textContent = up
      ? "5 free videos, no card needed."
      : "Sign in to keep your credits and history.";
    $("authSubmit").textContent = up ? "Create account" : "Sign in";
    $("password").setAttribute("autocomplete", up ? "new-password" : "current-password");
    $("authMsg").textContent = "";
  }

  function accountDate(value) {
    const date = value ? new Date(value) : null;
    return date && !Number.isNaN(date.getTime())
      ? date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
      : "Unknown time";
  }

  function deviceName(item, index) {
    return item.label || item.platform || `Device ${index + 1}`;
  }

  function setDeviceListMessage(message) {
    const list = $("accountDevices");
    list.replaceChildren();
    const note = document.createElement("span");
    note.className = "device-empty";
    note.textContent = message;
    list.appendChild(note);
  }

  function renderAccountDevices(devices) {
    const list = $("accountDevices");
    const count = $("accountDeviceCount");
    list.replaceChildren();
    count.textContent = `${devices.length} active`;
    if (!devices.length) {
      setDeviceListMessage("No active devices were found.");
      return;
    }
    devices.forEach((item, index) => {
      const row = document.createElement("div");
      row.className = "device-row";
      const detail = document.createElement("div");
      const name = document.createElement("b");
      name.textContent = deviceName(item, index);
      const meta = document.createElement("small");
      const platform = item.platform || "Browser device";
      meta.textContent = `${platform} · Last used ${accountDate(item.last_seen_at)}`;
      detail.append(name, meta);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "remove-device";
      remove.textContent = "Remove";
      remove.setAttribute("aria-label", `Remove ${deviceName(item, index)}`);
      remove.onclick = () => removeAccountDevice(item.id, remove, deviceName(item, index));
      row.append(detail, remove);
      list.appendChild(row);
    });
  }

  async function loadAccountDevices() {
    $("accountDeviceCount").textContent = "Loading...";
    setDeviceListMessage("Loading registered devices...");
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    try {
      const devices = await api("/auth/devices", { signal: controller.signal });
      renderAccountDevices(Array.isArray(devices) ? devices : []);
    } catch (e) {
      $("accountDeviceCount").textContent = "Unavailable";
      setDeviceListMessage(e.status === 401 ? "Sign in again to manage devices." : "Couldn't load devices. Close and reopen Account to try again.");
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function setBillingHistoryMessage(message) {
    const list = $("billingHistory");
    list.replaceChildren();
    const note = document.createElement("span");
    note.className = "billing-empty";
    note.textContent = message;
    list.appendChild(note);
  }

  function billingAmount(item) {
    if (!item.currency || typeof item.amount_subunits !== "number") return "Amount pending";
    try {
      return new Intl.NumberFormat(undefined, {
        style: "currency", currency: item.currency, maximumFractionDigits: 2,
      }).format(item.amount_subunits / 100);
    } catch (_) {
      return `${item.currency} ${(item.amount_subunits / 100).toFixed(2)}`;
    }
  }

  function renderBillingHistory(items) {
    const list = $("billingHistory");
    list.replaceChildren();
    if (!items.length) {
      setBillingHistoryMessage("No billing payments yet. Your invoices will appear here after a successful payment.");
      return;
    }
    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "billing-row";
      const detail = document.createElement("div");
      const amount = document.createElement("b");
      amount.textContent = billingAmount(item);
      const meta = document.createElement("small");
      const status = String(item.status || "pending").replace(/_/g, " ");
      meta.textContent = `${status} · ${accountDate(item.paid_at || item.created_at)}`;
      detail.append(amount, meta);
      if (item.invoice_url) {
        const invoice = document.createElement("a");
        invoice.href = item.invoice_url;
        invoice.target = "_blank";
        invoice.rel = "noopener noreferrer";
        invoice.textContent = "View invoice";
        row.append(detail, invoice);
      } else {
        const pending = document.createElement("span");
        pending.className = "billing-pending";
        pending.textContent = "Invoice pending";
        row.append(detail, pending);
      }
      list.appendChild(row);
    });
  }

  async function loadBillingHistory() {
    setBillingHistoryMessage("Loading billing history...");
    try {
      const history = await api("/billing/history");
      renderBillingHistory(Array.isArray(history) ? history : []);
    } catch (e) {
      setBillingHistoryMessage(e.status === 401 ? "Sign in again to view billing history." : "Couldn't load billing history. Close and reopen Account to try again.");
    }
  }

  async function removeAccountDevice(deviceId, button, name) {
    if (!window.confirm(`Remove ${name}? You can sign in again from that device later.`)) return;
    button.disabled = true;
    button.textContent = "Removing...";
    try {
      await api(`/auth/devices/${encodeURIComponent(deviceId)}`, { method: "DELETE" });
      $("acctMsg").textContent = "Device removed. You can now use that account slot on another device.";
      $("acctMsg").className = "msg ok";
      await loadAccountDevices();
    } catch (e) {
      $("acctMsg").textContent = e.message || "Couldn't remove this device.";
      $("acctMsg").className = "msg";
      button.disabled = false;
      button.textContent = "Remove";
    }
  }

  async function openAccount() {
    $("authModal").classList.remove("hidden");
    $("authPane").classList.add("hidden");
    $("acctPane").classList.remove("hidden");
    $("acctMsg").textContent = "";
    try {
      const [user, ent, plan] = await Promise.all([
        api("/auth/me"),
        api("/entitlement/check", { method: "POST", body: { device: device() } }),
        loadBillingPrice(),
      ]);
      accountUser = user;
      $("acctEmail").textContent = user.email;
      paintChip(ent);
      const pro = ent.plan === "subscription";
      const left = Math.min(ent.trials_remaining, ent.device_trials_remaining);
      $("acctStatus").textContent = pro
        ? "Subscription active"
        : left > 0 ? `${left} of ${ent.trials_limit} free videos left` : "Free videos used up";
      $("acctMeter").style.width = pro ? "100%" : `${((ent.trials_limit - left) / ent.trials_limit) * 100}%`;
      const price = priceForPlan(plan);
      $("acctDetail").textContent = pro
        ? (ent.current_period_end ? "Renews " + new Date(ent.current_period_end).toLocaleDateString() : `Billed ${price ? price.label : "monthly"}`)
        : "One video = one credit. Re-running a video you already did is free.";
      paintBillingPrice(pro);
      $("upgradeBtn").classList.toggle("hidden", pro);
      loadAccountDevices();
      loadBillingHistory();
      // The link itself is cosmetic. /admin and every data endpoint verify
      // the signed JWT on the server, so revealing it cannot grant access.
      api("/admin/session")
        .then(() => $("adminPanelBtn").classList.remove("hidden"))
        .catch(() => $("adminPanelBtn").classList.add("hidden"));
    } catch (e) {
      if (e.status === 401) { tokens.clear(); openAuth("login"); return; }
      $("acctMsg").textContent = "Can't reach the server.";
    }
  }

  function closeModal() { $("authModal").classList.add("hidden"); }

  function openOffer() {
    $("offerMsg").textContent = "";
    $("offerModal").classList.remove("hidden");
    $("offerClaim").focus();
  }

  function closeOffer() { $("offerModal").classList.add("hidden"); }

  async function startCheckout(offerCode) {
    const buttons = [$("upgradeBtn"), $("offerClaim"), $("offerRegular")];
    buttons.forEach((button) => { if (button) button.disabled = true; });
    $("offerMsg").textContent = "";
    try {
      const s = await api("/billing/checkout", {
        method: "POST", body: offerCode ? { offer_code: offerCode } : {},
      });
      closeOffer();
      window.open(s.checkout_url, "_blank", "noopener");
      $("acctMsg").textContent = "Finish the payment in the new tab, then reopen this panel.";
      $("acctMsg").className = "msg ok";
    } catch (e) {
      const message = e.status === 409
        ? "You already have an active subscription."
        : (e.message || "Couldn't start checkout.");
      $("acctMsg").textContent = message;
      $("acctMsg").className = "msg";
      if (!$("offerModal").classList.contains("hidden")) $("offerMsg").textContent = message;
    } finally {
      buttons.forEach((button) => { if (button) button.disabled = false; });
    }
  }

  async function beginSubscription() {
    try {
      if (!accountUser) throw new Error("Open your account again before continuing.");
      if (billingPrimarySiteUrl) {
        const primary = new URL(billingPrimarySiteUrl, window.location.origin);
        if (primary.origin !== window.location.origin) {
          window.location.assign(primary.href);
          return;
        }
      }
      // The offer is only presented for the server-selected INR tier. The
      // checkout endpoint repeats this country check before selecting Rs 99.
      if (currentBillingPlan && currentBillingPlan.currency === "INR") openOffer();
      else await startCheckout(null);
    } catch (e) {
      $("acctMsg").textContent = e.message || "Couldn't start checkout.";
      $("acctMsg").className = "msg";
    }
  }

  // =====================================================================
  // Wiring
  // =====================================================================
  $("goBtn").onclick = () => run(mode);
  $("summaryBtn").onclick = () => run("summary");
  // The hero button promises a full-notes PDF, regardless of the currently
  // selected output tab or an existing on-screen summary.
  $("pdfBtn").onclick = () => pdfFlow({ full: true });
  $("url").addEventListener("keydown", (e) => { if (e.key === "Enter") run(mode); });

  $("pasteBtn").onclick = async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) { $("url").value = text.trim(); $("url").focus(); }
    } catch (_) {
      $("url").focus();
      note("", "");
    }
  };

  $("themeBtn").onclick = () => {
    const dark = document.documentElement.dataset.theme === "dark";
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    $("themeBtn").textContent = dark ? "🌙" : "☀️";
    try { localStorage.setItem("tn_theme", dark ? "light" : "dark"); } catch (_) {}
  };

  const legalMenu = $("legalMenu");
  const legalMenuBtn = $("legalMenuBtn");
  function closeLegalMenu() {
    legalMenu.classList.add("hidden");
    legalMenuBtn.setAttribute("aria-expanded", "false");
  }
  legalMenuBtn.onclick = (event) => {
    event.stopPropagation();
    const willOpen = legalMenu.classList.contains("hidden");
    legalMenu.classList.toggle("hidden", !willOpen);
    legalMenuBtn.setAttribute("aria-expanded", String(willOpen));
  };
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".legal-menu")) closeLegalMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeLegalMenu();
  });

  $("accountBtn").onclick = () => (signedIn() ? openAccount() : openAuth("login"));
  $("tabIn").onclick = () => setAuthMode("login");
  $("tabUp").onclick = () => setAuthMode("signup");
  $("closeAuth").onclick = closeModal;
  $("closeAcct").onclick = closeModal;
  $("authModal").addEventListener("click", (e) => { if (e.target === $("authModal")) closeModal(); });

  $("authForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("authSubmit"), msg = $("authMsg");
    msg.textContent = ""; msg.className = "msg"; btn.disabled = true;
    try {
      const body = {
        email: $("email").value.trim(),
        password: $("password").value,
        device: device(),
      };
      if (authMode === "signup") {
        body.full_name = $("fullName").value.trim() || null;
      }
      const data = await api(authMode === "signup" ? "/auth/signup" : "/auth/login",
        { auth: false, method: "POST", body });
      tokens.set(data.tokens);
      paintChip(data.entitlement);
      setHeroBillingPrice(currentBillingPlan);
      closeModal();
      if ($("url").value.trim()) run(mode);
    } catch (e2) {
      msg.textContent = e2.status === 429
        ? "Too many attempts. Please wait a few minutes."
        : (e2.message || "Something went wrong.");
    } finally { btn.disabled = false; }
  });

  const forgotBtn = $("forgotBtn");
  if (forgotBtn) forgotBtn.onclick = async () => {
    const email = $("email").value.trim();
    const msg = $("authMsg");
    if (!email) { msg.textContent = "Enter your email first."; return; }
    try {
      const r = await api("/auth/password/forgot", { auth: false, method: "POST", body: { email } });
      msg.textContent = r.detail; msg.className = "msg ok";
    } catch (_) { msg.textContent = "Couldn't send the reset link."; }
  };

  $("signoutBtn").onclick = async () => {
    const t = tokens.get();
    try { if (t) await api("/auth/logout", { method: "POST", body: { refresh_token: t.refresh_token } }); } catch (_) {}
    tokens.clear(); paintChip(null); closeModal();
  };

  $("upgradeBtn").onclick = beginSubscription;
  $("offerClose").onclick = closeOffer;
  $("offerClaim").onclick = () => startCheckout("india_launch_99");
  $("offerRegular").onclick = () => startCheckout(null);
  $("offerModal").addEventListener("click", (e) => { if (e.target === $("offerModal")) closeOffer(); });

  // ---- Google sign-in -------------------------------------------------
  // Server /meta se batata hai ki feature chaalu hai ya nahi. Band ho to
  // yahan se aage kuch hota hi nahi aur purana form jaisa tha waisa rehta hai.
  async function initGoogle() {
    let meta;
    try { meta = await fetch(API + "/meta").then((r) => r.json()); }
    catch (_) { return; }
    billingPrimarySiteUrl = String(meta.billing_primary_site_url || "").trim();
    if (!meta.google_login || !meta.google_client_id) return;

    // Google ka script async load hota hai - taiyaar hone ka intezaar.
    for (let i = 0; i < 40 && !(window.google && google.accounts && google.accounts.id); i++) {
      await new Promise((r) => setTimeout(r, 150));
    }
    if (!(window.google && google.accounts && google.accounts.id)) return;

    google.accounts.id.initialize({
      client_id: meta.google_client_id,
      callback: onGoogleCredential,
      auto_select: false,
      cancel_on_tap_outside: true,
    });
    google.accounts.id.renderButton($("gsiButton"), {
      theme: document.documentElement.dataset.theme === "dark" ? "filled_black" : "outline",
      size: "large",
      width: 340,
      text: "continue_with",
      shape: "pill",
    });

    $("googleWrap").classList.remove("hidden");
    // Password wala form chhupa dete hain - link se kabhi bhi khul jaata hai.
    $("pwdWrap").classList.add("hidden");
  }

  async function onGoogleCredential(resp) {
    const msg = $("googleMsg");
    msg.textContent = "Signing you in\u2026";
    msg.className = "msg";
    try {
      const data = await api("/auth/google", {
        auth: false,
        method: "POST",
        body: {
          credential: resp.credential,
          device: device(),
        },
      });
      tokens.set(data.tokens);
      paintChip(data.entitlement);
      setHeroBillingPrice(currentBillingPlan);
      closeModal();
      if ($("url").value.trim()) run(mode);
    } catch (e) {
      msg.textContent = e.status === 429
        ? "Too many attempts. Please wait a few minutes."
        : (e.message || "Google sign-in failed.");
      msg.className = "msg";
    }
  }

  const showPwd = $("showPwd");
  if (showPwd) showPwd.onclick = () => {
    const opening = $("pwdWrap").classList.contains("hidden");
    $("pwdWrap").classList.toggle("hidden");
    showPwd.textContent = opening
      ? "Hide email & password"
      : "Continue with email & password";
    if (opening) $("email").focus();
  };

  // ---- boot ----
  if ($("outLang")) fillLangSelect($("outLang"), { includeAuto: true });
  initGoogle();
  loadBillingPrice().catch(() => {});
  try {
    const t = localStorage.getItem("tn_theme");
    if (t) { document.documentElement.dataset.theme = t; $("themeBtn").textContent = t === "dark" ? "☀️" : "🌙"; }
  } catch (_) {}
  refreshEntitlement();
})();
