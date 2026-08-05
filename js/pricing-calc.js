(function () {
  var TIERS = [1, 5, 10, 15];
  // Mandatory base tabs — always included, can't be unchecked. Part of every total.
  var CORE_PRICES = {
    home:       [49, 179, 339, 419],
    products:   [109, 459, 839, 1079],
    categories: [69, 279, 509, 639],
    suppliers:  [59, 229, 419, 539],
    orders:     [269, 1159, 2119, 2699]
  };
  var TAB_PRICES = {
    payments:  [329, 699, 1299, 1649],
    cost:      [379, 839, 1539, 1969],
    packaging: [219, 459, 859, 1069],
    files:     [169, 359, 679, 869],
    coststock: [329, 699, 1299, 1649],
    packstock: [219, 459, 859, 1069],
    mediaplan: [269, 599, 1119, 1419],
    schedule:  [329, 699, 1299, 1649],
    notebook:  [119, 229, 439, 539],
    calc:      [119, 229, 439, 539]
  };
  var SEAT_PRICES = {
    local: [119, 299, 599, 869],
    vps:   [719, 3129, 6099, 8319]
  };
  var HOSTING_FEE = [999, 1299, 1599, 2399];
  var HOSTING_SETUP = 1199;
  var GSM_PRICE = 3999;
  var GSM_SETUP = 1599;
  var USD_RATE = 41.5;

  function fmtUah(n) {
    return n.toLocaleString("uk-UA") + " ₴";
  }
  function roundUpTo9(n) {
    return Math.ceil((n + 1) / 10) * 10 - 1;
  }
  function fmtUsd(uah) {
    return "$" + Math.round(uah / USD_RATE);
  }

  function recalc() {
    var peopleSel = document.getElementById("calcPeople");
    var modeSel = document.getElementById("calcMode");
    var totalEl = document.getElementById("calcTotal");
    var onceEl = document.getElementById("calcOnce");
    var vpsNoteEl = document.getElementById("calcVpsNote");
    if (!peopleSel || !modeSel || !totalEl) return;

    var tierIndex = TIERS.indexOf(Number(peopleSel.value));
    if (tierIndex === -1) tierIndex = 0;
    var mode = modeSel.value;

    var total = mode === "local" ? SEAT_PRICES.local[tierIndex] : SEAT_PRICES.vps[tierIndex];
    var once = 0;

    for (var key in CORE_PRICES) {
      total += CORE_PRICES[key][tierIndex];
    }

    var tabBoxes = document.querySelectorAll(".calc-tab");
    for (var i = 0; i < tabBoxes.length; i++) {
      if (tabBoxes[i].checked) {
        total += TAB_PRICES[tabBoxes[i].dataset.key][tierIndex];
      }
    }

    var gsmBox = document.getElementById("calcGsm");
    if (gsmBox && gsmBox.checked) {
      total += GSM_PRICE;
      once += GSM_SETUP;
    }

    if (mode === "vpsours") {
      total += HOSTING_FEE[tierIndex];
      once += HOSTING_SETUP;
    }
    if (vpsNoteEl) {
      vpsNoteEl.style.display = mode === "vpsours" ? "" : "none";
    }

    total = roundUpTo9(total);
    if (once > 0) once = roundUpTo9(once);

    totalEl.textContent = fmtUah(total) + " / " + fmtUsd(total) + " /міс";

    if (onceEl) {
      onceEl.textContent = once > 0 ? "+ " + fmtUah(once) + " / " + fmtUsd(once) + " одноразово" : "";
    }
  }

  // ---- Live USD rate (NBU, refreshed once per day, client-side only) ----
  var NBU_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json";
  var RATE_CACHE_KEY = "catman_usd_rate_v1";

  function todayStr() {
    var d = new Date();
    return d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate();
  }

  function refreshStaticUsdPrices() {
    var els = document.querySelectorAll(".price-usd");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var host = el.parentElement;
      if (!host) continue;
      var m = /(\d[\d\s]*)\s*₴/.exec(host.textContent);
      if (!m) continue;
      var uah = parseInt(m[1].replace(/\s/g, ""), 10);
      if (!uah) continue;
      el.textContent = el.textContent.replace(/\$[\d.]+/, "$" + (Math.round(uah / USD_RATE * 2) / 2));
    }
  }

  function applyRate(rate) {
    if (!rate || rate <= 0) return;
    USD_RATE = rate;
    refreshStaticUsdPrices();
    recalc();
  }

  function initUsdRate() {
    var cached = null;
    try {
      cached = JSON.parse(localStorage.getItem(RATE_CACHE_KEY) || "null");
    } catch (e) {
      cached = null;
    }
    if (cached && cached.date === todayStr() && cached.rate) {
      applyRate(cached.rate);
      return; // already today's rate — no need to hit the network again
    }
    // Show the built-in fallback rate immediately, then swap in the live
    // one once (if) the fetch succeeds — never blocks/delays the page.
    fetch(NBU_URL)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var rate = data && data[0] && data[0].rate;
        if (!rate) return;
        applyRate(rate);
        try {
          localStorage.setItem(RATE_CACHE_KEY, JSON.stringify({ rate: rate, date: todayStr() }));
        } catch (e) { /* localStorage unavailable — just skip caching */ }
      })
      .catch(function () { /* offline/NBU down — keep the fallback rate */ });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var watched = document.querySelectorAll("#calcPeople, #calcMode, .calc-tab, #calcGsm");
    for (var i = 0; i < watched.length; i++) {
      watched[i].addEventListener("change", recalc);
    }
    recalc();
    initUsdRate();
  });
})();
