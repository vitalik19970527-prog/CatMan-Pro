(function () {
  var TIERS = [1, 5, 10, 15];
  // Mandatory base tabs — always included, can't be unchecked. Part of every total.
  var CORE_PRICES = {
    home:       [29, 149, 279, 349],
    products:   [69, 379, 699, 899],
    categories: [49, 229, 419, 529],
    suppliers:  [39, 189, 349, 449],
    orders:     [199, 959, 1759, 2249]
  };
  var TAB_PRICES = {
    payments:  [239, 579, 1079, 1369],
    cost:      [279, 699, 1279, 1639],
    packaging: [159, 379, 709, 889],
    files:     [119, 299, 559, 719],
    coststock: [239, 579, 1079, 1369],
    packstock: [159, 379, 709, 889],
    mediaplan: [199, 499, 929, 1179],
    schedule:  [239, 579, 1079, 1369],
    notebook:  [79, 189, 359, 449],
    calc:      [79, 189, 359, 449]
  };
  var SEAT_PRICES = {
    local: [79, 249, 499, 719],
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

    total = roundUpTo9(total);
    if (once > 0) once = roundUpTo9(once);

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
