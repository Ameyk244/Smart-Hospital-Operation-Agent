import "@testing-library/jest-dom/vitest";

// jsdom doesn't implement scrollIntoView; useRowHighlight (and anything
// else that scrolls a matched row into view) calls it unconditionally, so
// tests need a no-op stand-in rather than a per-file mock.
if (typeof Element.prototype.scrollIntoView !== "function") {
  Element.prototype.scrollIntoView = function scrollIntoView() {
    // no-op: jsdom doesn't implement layout/scrolling.
  };
}
