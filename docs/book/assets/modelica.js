/* Read-only source panels using the same Modelica grammar as Rumoca's book. */
(() => {
  "use strict";
  const blocks = [...document.querySelectorAll("pre > code.language-modelica")];
  if (!blocks.length) return;

  /* mdBook defines path_to_root for both local previews and Pages subpaths. */
  const vendor = new URL(`${path_to_root}vendor/`, location.href);
  const isDark = () => ["navy", "coal", "ayu"].some(theme =>
    document.documentElement.classList.contains(theme));
  let loading;
  function loadMonaco() {
    if (!loading) loading = (async () => {
      const base = new URL("monaco/vs/", vendor);
      await new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = new URL("loader.js", base).href;
        script.onload = resolve;
        script.onerror = () => reject(new Error("Cannot load the Modelica viewer"));
        document.head.append(script);
      });
      window.require.config({ paths: { vs: base.href.replace(/\/$/, "") } });
      await new Promise((resolve, reject) =>
        window.require(["vs/editor/editor.main"], resolve, reject));
      const { registerModelicaLanguage } = await import(
        new URL("rumoca/modelica_language.js", vendor).href);
      registerModelicaLanguage(window.monaco);
      const syncTheme = () => window.monaco.editor.setTheme(isDark() ? "vs-dark" : "vs");
      syncTheme();
      new MutationObserver(syncTheme).observe(document.documentElement, {
        attributes: true, attributeFilter: ["class"],
      });
      return window.monaco;
    })();
    return loading;
  }

  async function enhance(code) {
    // Read text from the included source, never from a second hand-maintained model.
    const source = code.textContent.replace(/\n$/, "");
    const pre = code.parentElement;
    let panel;
    try {
      const monaco = await loadMonaco();
      const name = source.match(/^\s*(?:(?:partial|encapsulated)\s+)*(?:model|package|function|record|block|class)\s+(\w+)/m)?.[1];
      const scope = source.match(/^within\s+([^;]+);/m)?.[1];
      const label = name ? `${scope ? `${scope}.` : ""}${name}` : "Modelica";
      panel = document.createElement("section");
      panel.className = "modelica-viewer";
      panel.setAttribute("aria-label", `${label} source code`);
      const toolbar = document.createElement("div");
      toolbar.className = "modelica-toolbar";
      const title = document.createElement("span");
      title.textContent = label;
      const copy = document.createElement("button");
      copy.type = "button";
      copy.textContent = "Copy code";
      copy.setAttribute("aria-label", `Copy ${label} source code`);
      const status = document.createElement("span");
      status.className = "modelica-status";
      status.setAttribute("role", "status");
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(source + "\n");
          status.textContent = "Copied";
        } catch {
          status.textContent = "Select the code and copy with Ctrl+C or ⌘C.";
        }
        setTimeout(() => { status.textContent = ""; }, 4000);
      });
      toolbar.append(title, status, copy);
      const host = document.createElement("div");
      host.className = "modelica-editor";
      host.style.height = `${Math.min(560, source.split("\n").length * 21 + 24)}px`;
      panel.append(toolbar, host);
      pre.before(panel);
      const editor = monaco.editor.create(host, {
        value: source, language: "modelica", readOnly: true, domReadOnly: true,
        ariaLabel: `${label} Modelica source (read only)`,
        fontSize: 14, lineHeight: 21, fontFamily: "var(--mono-font)",
        minimap: { enabled: false }, lineNumbersMinChars: 3,
        scrollBeyondLastLine: false, renderLineHighlight: "none",
        wordWrap: "on", wrappingIndent: "same", automaticLayout: true,
        folding: true, showFoldingControls: "always", glyphMargin: false,
        contextmenu: false, links: false, overviewRulerLanes: 0,
        hideCursorInOverviewRuler: true, occurrencesHighlight: "off",
        selectionHighlight: false, padding: { top: 12, bottom: 12 },
        scrollbar: { alwaysConsumeMouseWheel: false },
      });
      let lastHeight = 0;
      function fit() {
        const height = Math.min(560, editor.getContentHeight());
        if (height !== lastHeight) {
          lastHeight = height;
          host.style.height = `${height}px`;
          editor.layout();
        }
      }
      editor.onDidContentSizeChange(fit);
      fit();
      pre.hidden = true; // Keep the ordinary code block for print and no-JS reading.
      panel.classList.add("modelica-ready");
    } catch (error) {
      panel?.remove();
      console.warn("Modelica viewer unavailable; keeping the source block.", error);
    }
  }

  // Closed <details> and models below the fold do not load editors until needed.
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) {
      observer.unobserve(entry.target);
      enhance(entry.target);
    }
  }, { rootMargin: "200px" });
  blocks.forEach(code => observer.observe(code));
})();
