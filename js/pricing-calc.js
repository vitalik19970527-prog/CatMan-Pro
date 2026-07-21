(function () {
  var TIERS = [1, 5, 10, 15];
  // Mandatory base tabs — always included, can't be unchecked. Part of every total.
  var CORE_PRICES = {
    home:       [40, 185, 345, 440],
    products:   [90, 480, 870, 1125],
    categories: [60, 285, 525, 660],
    suppliers:  [50, 240, 435, 560],
    orders:     [255, 1195, 2200, 2810]
  };
  var TAB_PRICES = {
    payments:  [295, 725, 1345, 1710],
    cost:      [355, 870, 1600, 2050],
    packaging: [195, 480, 890, 1110],
    files:     [155, 380, 705, 905],
    coststock: [295, 725, 1345, 1710],
    packstock: [195, 480, 890, 1110],
    mediaplan: [255, 625, 1160, 1470],
    schedule:  [295, 725, 1345, 1710],
    notebook:  [95, 240, 445, 560],
    calc:      [95, 240, 445, 560]
  };
  var SEAT_PRICES = {
    local: [104, 314, 624, 904],
    vps:   [904, 3914, 7624, 10404]
  };
  var HOSTING_FEE = [1000, 1200, 2000, 3000];
  var HOSTING_SETUP = 1499;
  var GSM_PRICE = 4999;
  var GSM_SETUP = 1999;
  var USD_RATE = 41.5;

  function fmtUah(n) {
    return n.toLocaleString("uk-UA") + " ₴";
  }
  function fmtUsd(uah) {
    return "$" + Math.round(uah / USD_RATE);
  }

  function recalc() {
    var peopleSel = document.getElementById("calcPeople");
    var modeSel = document.getElementById("calcMode");
    var totalEl = document.getElementById("calcTotal");
    var onceEl = document.getElementById("calcOnce");
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

    totalEl.textContent = fmtUah(total) + " / " + fmtUsd(total) + " /міс";

    if (onceEl) {
      onceEl.textContent = once > 0 ? "+ " + fmtUah(once) + " / " + fmtUsd(once) + " одноразово" : "";
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var watched = document.querySelectorAll("#calcPeople, #calcMode, .calc-tab, #calcGsm");
    for (var i = 0; i < watched.length; i++) {
      watched[i].addEventListener("change", recalc);
    }
    recalc();
  });
})();
