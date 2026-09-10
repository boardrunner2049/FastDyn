/* Render fenced Mermaid diagrams and follow the mdBook color theme. */
(() => {
  "use strict";
  const diagrams = [...document.querySelectorAll("pre > code.language-mermaid")]
    .map(code => ({ node: code.parentElement, source: code.textContent }));
  if (!diagrams.length) return;
  const theme = () => ["navy", "coal", "ayu"].some(name =>
    document.documentElement.classList.contains(name)) ? "dark" : "default";
  let previous, pending = Promise.resolve();
  function render() {
    const current = theme();
    if (current === previous) return;
    previous = current;
    pending = pending.then(async () => {
      const dark = current === "dark";
      mermaid.initialize({
        startOnLoad: false, theme: "base",
        themeVariables: {
          darkMode: dark, fontFamily: "system-ui, sans-serif", fontSize: "16px",
          primaryColor: dark ? "#1a3544" : "#eaf4f6",
          primaryTextColor: dark ? "#dbe6f2" : "#203149",
          primaryBorderColor: dark ? "#69bbca" : "#248092",
          lineColor: dark ? "#94bdcf" : "#57798b",
          secondaryColor: dark ? "#233753" : "#edf1fa",
          tertiaryColor: dark ? "#17263a" : "#ffffff",
          edgeLabelBackground: dark ? "#17263a" : "#ffffff",
        },
        flowchart: { curve: "basis", padding: 20, nodeSpacing: 28, rankSpacing: 38 },
      });
      for (const { node, source } of diagrams) {
        node.removeAttribute("data-processed");
        node.textContent = source;
        node.classList.add("mermaid");
      }
      await mermaid.run({ nodes: diagrams.map(diagram => diagram.node) });
    }).catch(error => console.error("Mermaid rendering failed", error));
  }
  render();
  new MutationObserver(render).observe(document.documentElement, {
    attributes: true, attributeFilter: ["class"],
  });
})();
