document.getElementById("year").textContent = new Date().getFullYear();

const navToggle = document.getElementById("navToggle");
const nav = document.querySelector(".nav");

navToggle.addEventListener("click", () => {
  const isOpen = nav.classList.toggle("nav-open");
  navToggle.setAttribute("aria-expanded", String(isOpen));
});

document.querySelectorAll(".nav a").forEach((link) => {
  link.addEventListener("click", () => {
    nav.classList.remove("nav-open");
    navToggle.setAttribute("aria-expanded", "false");
  });
});

const langBtn = document.getElementById("langBtn");
const langList = document.getElementById("langList");

langBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  const isOpen = langList.classList.toggle("open");
  langBtn.setAttribute("aria-expanded", String(isOpen));
});

document.addEventListener("click", (e) => {
  if (!langList.contains(e.target) && e.target !== langBtn) {
    langList.classList.remove("open");
    langBtn.setAttribute("aria-expanded", "false");
  }
});

/* ---------- screenshot lightbox ---------- */
const lightbox = document.getElementById("lightbox");
const lightboxTitle = document.getElementById("lightboxTitle");
const lightboxImg = document.getElementById("lightboxImg");
const lightboxPlaceholder = document.getElementById("lightboxPlaceholder");
const lightboxPlaceholderTitle = document.getElementById("lightboxPlaceholderTitle");
const lightboxClose = document.getElementById("lightboxClose");

function shotTitle(el) {
  const inner = el.querySelector("strong, h5");
  return inner ? inner.textContent.trim() : "";
}

function openLightbox(el) {
  const file = el.getAttribute("data-shot");
  const title = shotTitle(el);
  const src = (window.ASSET_PREFIX || "") + "img/" + file;
  lightboxTitle.textContent = title;
  lightboxPlaceholderTitle.textContent = title;

  lightboxImg.style.display = "none";
  lightboxPlaceholder.style.display = "flex";

  const probe = new Image();
  probe.onload = () => {
    lightboxImg.src = src;
    lightboxImg.alt = title;
    lightboxImg.style.display = "block";
    lightboxPlaceholder.style.display = "none";
  };
  probe.src = src;

  lightbox.classList.add("open");
  document.body.style.overflow = "hidden";
  lightboxClose.focus();
}

function closeLightbox() {
  lightbox.classList.remove("open");
  document.body.style.overflow = "";
  lightboxImg.src = "";
}

document.querySelectorAll("[data-shot]").forEach((el) => {
  el.addEventListener("click", () => openLightbox(el));
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openLightbox(el);
    }
  });
});

/* Show the real screenshot right in the small frame instead of just the
   "СКРІНШОТ" placeholder — previously the image only loaded once you
   clicked through to the lightbox. Probes each file the same way
   openLightbox() does; frames whose file isn't captured yet (most of
   them, still TODO) silently keep the placeholder. The placeholder stays
   in the DOM (just hidden) so shotTitle() can still read its <strong> for
   the lightbox title. */
document.querySelectorAll("[data-shot]").forEach((el) => {
  const placeholder = el.querySelector(".shot-placeholder");
  if (!placeholder) return;
  const file = el.getAttribute("data-shot");
  const src = (window.ASSET_PREFIX || "") + "img/" + file;

  const probe = new Image();
  probe.onload = () => {
    const img = document.createElement("img");
    img.className = "shot-thumb";
    img.src = src;
    img.alt = shotTitle(el);
    placeholder.style.display = "none";
    el.insertBefore(img, placeholder);
  };
  probe.src = src;
});

lightboxClose.addEventListener("click", closeLightbox);
lightbox.addEventListener("click", (e) => {
  if (e.target === lightbox) closeLightbox();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && lightbox.classList.contains("open")) closeLightbox();
});
