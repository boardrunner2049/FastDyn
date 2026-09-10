/* Keep mdBook's native chapter folding usable with a keyboard and screen reader. */
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".chapter-fold-toggle").forEach(toggle => {
    const item = toggle.closest("li");
    const label = item.querySelector(".chapter-link-wrapper > a").textContent.trim();
    toggle.setAttribute("role", "button");
    toggle.setAttribute("tabindex", "0");
    toggle.setAttribute("aria-label", `Expand or collapse ${label}`);
    const update = () => toggle.setAttribute("aria-expanded", item.classList.contains("expanded"));
    update();
    new MutationObserver(update).observe(item, {attributes: true, attributeFilter: ["class"]});
    toggle.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle.click();
      }
    });
  });
});
