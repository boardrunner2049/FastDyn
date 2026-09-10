/* Stable chapter/section labels; existing heading IDs remain bookmarkable. */
document.addEventListener("DOMContentLoaded", () => {
  const active = document.querySelector("#mdbook-sidebar a.active");
  const chapter = active?.querySelector("strong")?.textContent.trim().replace(/\.$/, "");
  if (!chapter) return;
  const counters = [0, 0, 0, 0, 0];
  for (const heading of document.querySelectorAll("main h1, main h2, main h3, main h4, main h5, main h6")) {
    const level = Number(heading.tagName[1]) - 2;
    let number = chapter;
    if (level >= 0) {
      for (let i = 0; i < level; i++) if (!counters[i]) counters[i] = 1;
      counters[level]++;
      counters.fill(0, level + 1);
      number += "." + counters.slice(0, level + 1).join(".");
    }
    const anchor = heading.querySelector("a.header") || heading;
    const first = anchor.firstChild;
    if (first?.nodeType === Node.TEXT_NODE) first.textContent = first.textContent.replace(/^\d+\.\s+/, "");
    const prefix = document.createElement("span");
    prefix.className = "section-label";
    prefix.textContent = number + " ";
    anchor.prepend(prefix);
    for (const link of document.querySelectorAll(".header-in-summary")) {
      if (decodeURIComponent(link.hash.slice(1)) === heading.id) link.textContent = anchor.textContent;
    }
  }
});
