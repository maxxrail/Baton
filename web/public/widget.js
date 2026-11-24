/*
 * Baton chat widget embed snippet. Add one tag to any page:
 *
 *   <script src="https://YOUR-BATON-WEB/widget.js" async></script>
 *
 * It mounts the widget in an iframe served from the widget's own origin, so the host page's
 * CSS can't break it and it can't read the host page. The iframe is a small launcher until
 * the customer opens the chat; the widget tells us (postMessage) when to grow or shrink.
 * Set data-baton-origin on the tag to load the widget from somewhere else.
 */
(function () {
  var script = document.currentScript;
  var origin = (script && script.getAttribute("data-baton-origin")) || new URL(script.src).origin;
  if (window.__batonWidget) return; // loaded twice
  window.__batonWidget = true;

  var frame = document.createElement("iframe");
  frame.src = origin + "/widget?embed=1";
  frame.title = "Northwind Goods chat";
  frame.setAttribute("allowtransparency", "true");
  var closed = { width: "96px", height: "96px" };
  var opened = { width: "min(410px, 100vw)", height: "min(660px, 100vh)" };
  Object.assign(frame.style, closed, {
    position: "fixed",
    right: "0",
    bottom: "0",
    border: "0",
    background: "transparent",
    colorScheme: "normal",
    zIndex: "2147483000",
  });

  window.addEventListener("message", function (event) {
    if (event.origin !== origin || event.source !== frame.contentWindow) return;
    var data = event.data || {};
    if (data.type === "baton:resize") Object.assign(frame.style, data.open ? opened : closed);
  });

  function mount() {
    document.body.appendChild(frame);
  }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
