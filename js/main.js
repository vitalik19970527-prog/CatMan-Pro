(function () {
  "use strict";

  var yearEl = document.getElementById("year");
  if (yearEl) yearEl.textContent = new Date().getFullYear();

  // ---- lightbox for screenshots ----
  var overlay = document.getElementById("lightbox");
  if (overlay) {
    var img = document.getElementById("lightboxImg");
    var titleEl = document.getElementById("lightboxTitle");
    var closeBtn = document.getElementById("lightboxClose");

    function openShot(frame) {
      var src = frame.getAttribute("data-shot");
      var title = frame.querySelector("strong");
      if (!src) return;
      img.src = "img/" + src;
      img.alt = title ? title.textContent : "";
      titleEl.textContent = title ? title.textContent : "";
      overlay.classList.add("is-open");
    }
    function closeShot() {
      overlay.classList.remove("is-open");
      img.src = "";
    }

    document.querySelectorAll(".shot-frame").forEach(function (frame) {
      frame.addEventListener("click", function () { openShot(frame); });
      frame.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openShot(frame);
        }
      });
    });
    closeBtn.addEventListener("click", closeShot);
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) closeShot();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeShot();
    });
  }
})();
