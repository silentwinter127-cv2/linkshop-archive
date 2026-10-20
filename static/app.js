(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  // 搜索快捷键：/ 聚焦，Esc 清空。
  const headerSearch = $(".header-search input");
  document.addEventListener("keydown", (event) => {
    const tag = (event.target && event.target.tagName) || "";
    const typing = /INPUT|TEXTAREA|SELECT/.test(tag) || event.target?.isContentEditable;
    if (event.key === "/" && !typing && headerSearch) {
      event.preventDefault();
      headerSearch.focus();
      headerSearch.select();
    }
    if (event.key === "Escape" && document.activeElement === headerSearch) {
      headerSearch.value = "";
      headerSearch.blur();
    }
  });

  // 从搜索结果跳转过来时，高亮对应图版。
  const highlightHash = () => {
    const id = decodeURIComponent(window.location.hash.replace(/^#/, ""));
    if (!id.startsWith("image-")) return;
    const target = document.getElementById(id);
    if (!target) return;
    requestAnimationFrame(() => {
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.classList.add("is-target");
      window.setTimeout(() => target.classList.remove("is-target"), 4200);
    });
  };
  window.addEventListener("hashchange", highlightHash);
  highlightHash();

  const lightbox = $("#lightbox");
  if (!lightbox) return;

  // 移出 main 的层叠上下文，确保灯箱覆盖固定页头。
  document.body.appendChild(lightbox);

  const shots = $$(".shot");
  if (!shots.length) return;

  const image = $("#lb-image");
  const loader = $("#lb-loader");
  const heading = $("#lb-heading");
  const pageTitle = $("#lb-page-title");
  const floor = $("#lb-floor");
  const archived = $("#lb-archived");
  const posted = $("#lb-posted");
  const exif = $("#lb-exif");
  const spec = $("#lb-spec");
  const sha = $("#lb-sha");
  const count = $("#lb-count");
  const download = $("#lb-download");
  const source = $("#lb-source");
  const original = $("#lb-original");
  const currentEl = $("#lb-current");
  const totalEl = $("#lb-total");
  const closeButton = $(".lb-close");
  const stage = $(".lb-stage");

  let current = -1;
  let lastFocus = null;
  let touchStartX = 0;

  totalEl.textContent = String(shots.length);

  const value = (figure, key) => figure.dataset[key] || "—";

  const preload = (index) => {
    if (index < 0 || index >= shots.length) return;
    const img = new Image();
    img.src = shots[index].dataset.full;
  };

  const render = (index) => {
    const figure = shots[index];
    if (!figure) return;
    current = index;

    image.classList.remove("is-ready");
    loader.style.display = "block";
    loader.textContent = "载入大图…";
    image.alt = value(figure, "pageTitle");
    image.src = figure.dataset.full;

    heading.textContent = value(figure, "floor");
    pageTitle.textContent = `《${value(figure, "pageTitle")}》`;
    floor.textContent = value(figure, "floor");
    archived.textContent = value(figure, "archived");
    posted.textContent = value(figure, "posted");
    exif.textContent = value(figure, "exif");
    spec.textContent = value(figure, "spec");
    sha.textContent = value(figure, "sha");
    count.textContent = value(figure, "count");
    download.href = figure.dataset.download;
    source.href = figure.dataset.source;
    original.href = figure.dataset.original;
    currentEl.textContent = String(index + 1);

    preload(index - 1);
    preload(index + 1);
  };

  const open = (index) => {
    lastFocus = document.activeElement;
    lightbox.classList.add("is-open");
    lightbox.setAttribute("aria-hidden", "false");
    document.body.classList.add("lb-open");
    render(index);
    closeButton.focus({ preventScroll: true });
  };

  const close = () => {
    lightbox.classList.remove("is-open");
    lightbox.setAttribute("aria-hidden", "true");
    document.body.classList.remove("lb-open");
    image.removeAttribute("src");
    image.classList.remove("is-ready");
    if (lastFocus && typeof lastFocus.focus === "function") {
      lastFocus.focus({ preventScroll: true });
    }
  };

  const step = (delta) => {
    if (!lightbox.classList.contains("is-open")) return;
    const next = (current + delta + shots.length) % shots.length;
    render(next);
  };

  shots.forEach((figure, index) => {
    const button = $(".shot-button", figure);
    if (button) button.addEventListener("click", () => open(index));
  });

  $$("[data-close]", lightbox).forEach((node) => node.addEventListener("click", close));
  $(".lb-prev", lightbox)?.addEventListener("click", () => step(-1));
  $(".lb-next", lightbox)?.addEventListener("click", () => step(1));

  image.addEventListener("load", () => {
    loader.style.display = "none";
    image.classList.add("is-ready");
  });

  image.addEventListener("error", () => {
    loader.textContent = "图片文件缺失或无法载入";
    image.classList.remove("is-ready");
  });

  stage.addEventListener("touchstart", (event) => {
    touchStartX = event.changedTouches[0].clientX;
  }, { passive: true });

  stage.addEventListener("touchend", (event) => {
    const delta = event.changedTouches[0].clientX - touchStartX;
    if (Math.abs(delta) > 55) step(delta > 0 ? -1 : 1);
  }, { passive: true });

  document.addEventListener("keydown", (event) => {
    if (!lightbox.classList.contains("is-open")) return;
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      step(-1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      step(1);
    } else if (event.key === "Tab") {
      const focusables = $$(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        lightbox
      ).filter((node) => node.offsetParent !== null);
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });
})();
