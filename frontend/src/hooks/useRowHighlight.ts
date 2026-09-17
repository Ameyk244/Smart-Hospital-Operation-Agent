import { useEffect, useRef } from "react";

/**
 * Shared behavior for the Operations panel's "row touched by this chat
 * turn" highlight (see OperationsView's touchedEntityCodes prop docs):
 * scrolls the first matching row into view, and forces the `.row-touched`
 * flash animation (see App.css) to restart even when the exact same row
 * was already highlighted by the previous turn.
 *
 * That restart matters because touchedEntityCodes is a fresh array on
 * every chat turn (App.tsx replaces it wholesale), but if the same code
 * appears in two consecutive turns the row's className string stays
 * "row-touched" across the re-render — React only touches the DOM
 * attribute when the value actually changes, so merely re-deriving the
 * same className would never replay the CSS animation. Removing the
 * class, forcing a layout read, then re-adding it works around that.
 *
 * Each panel (DepartmentsPanel, ScannersPanel, AppointmentsPanel,
 * PatientSearchPanel) calls this with its own rows + code accessor and
 * gets back `rowRef(code)`, a ref callback to attach to each `<tr>`.
 */
export function useRowHighlight<T>(
  rows: T[],
  getCode: (row: T) => string,
  touchedEntityCodes: string[],
) {
  const nodesByCode = useRef(new Map<string, HTMLTableRowElement>());

  useEffect(() => {
    if (touchedEntityCodes.length === 0) return;
    const match = rows.find((row) => touchedEntityCodes.includes(getCode(row)));
    if (!match) return;
    const el = nodesByCode.current.get(getCode(match));
    if (!el) return;

    el.classList.remove("row-touched");
    // Force a reflow between the remove and the re-add below so the two
    // class mutations don't coalesce into a no-op — that's what lets the
    // @keyframes animation restart on a repeat highlight of the same row.
    void el.offsetWidth;
    el.classList.add("row-touched");

    el.scrollIntoView({ behavior: "smooth", block: "center" });
    // touchedEntityCodes (a fresh array each chat turn) is the intentional
    // trigger; rows/getCode changing independently shouldn't replay this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [touchedEntityCodes]);

  function rowRef(code: string) {
    return (el: HTMLTableRowElement | null) => {
      if (el) {
        nodesByCode.current.set(code, el);
      } else {
        nodesByCode.current.delete(code);
      }
    };
  }

  return { rowRef };
}
