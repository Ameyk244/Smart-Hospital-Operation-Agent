import { describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useRowHighlight } from "./useRowHighlight";

interface Row {
  code: string;
}

function makeRow(): HTMLTableRowElement {
  return document.createElement("tr");
}

describe("useRowHighlight", () => {
  it("exposes a rowRef callback", () => {
    const rows: Row[] = [{ code: "A" }];
    const { result } = renderHook(
      ({ touched }: { touched: string[] }) =>
        useRowHighlight(rows, (r) => r.code, touched),
      { initialProps: { touched: [] as string[] } },
    );
    expect(typeof result.current.rowRef).toBe("function");
  });

  it("scrolls the first matching row into view when touchedEntityCodes changes", () => {
    const rows: Row[] = [{ code: "A" }, { code: "B" }];
    const elA = makeRow();
    const elB = makeRow();
    const scrollA = vi.spyOn(elA, "scrollIntoView");
    const scrollB = vi.spyOn(elB, "scrollIntoView");

    const { result, rerender } = renderHook(
      ({ touched }: { touched: string[] }) =>
        useRowHighlight(rows, (r) => r.code, touched),
      { initialProps: { touched: [] as string[] } },
    );
    result.current.rowRef("A")(elA);
    result.current.rowRef("B")(elB);

    rerender({ touched: ["B"] });

    expect(scrollB).toHaveBeenCalledWith({ behavior: "smooth", block: "center" });
    expect(scrollA).not.toHaveBeenCalled();
  });

  it("only scrolls the first row that matches when several codes are touched", () => {
    const rows: Row[] = [{ code: "A" }, { code: "B" }];
    const elA = makeRow();
    const elB = makeRow();
    const scrollA = vi.spyOn(elA, "scrollIntoView");
    const scrollB = vi.spyOn(elB, "scrollIntoView");

    const { result, rerender } = renderHook(
      ({ touched }: { touched: string[] }) =>
        useRowHighlight(rows, (r) => r.code, touched),
      { initialProps: { touched: [] as string[] } },
    );
    result.current.rowRef("A")(elA);
    result.current.rowRef("B")(elB);

    rerender({ touched: ["A", "B"] });

    expect(scrollA).toHaveBeenCalledTimes(1);
    expect(scrollB).not.toHaveBeenCalled();
  });

  it("does nothing when no row matches touchedEntityCodes", () => {
    const rows: Row[] = [{ code: "A" }];
    const elA = makeRow();
    const scrollA = vi.spyOn(elA, "scrollIntoView");

    const { result, rerender } = renderHook(
      ({ touched }: { touched: string[] }) =>
        useRowHighlight(rows, (r) => r.code, touched),
      { initialProps: { touched: [] as string[] } },
    );
    result.current.rowRef("A")(elA);

    rerender({ touched: ["does-not-exist"] });

    expect(scrollA).not.toHaveBeenCalled();
  });

  it("removes and re-adds the row-touched class to restart the flash, even for a repeat match", () => {
    const rows: Row[] = [{ code: "A" }];
    const elA = makeRow();
    // Simulate React having already applied the class from a previous turn
    // that touched the same code.
    elA.classList.add("row-touched");

    const { result, rerender } = renderHook(
      ({ touched }: { touched: string[] }) =>
        useRowHighlight(rows, (r) => r.code, touched),
      { initialProps: { touched: ["A"] } },
    );
    result.current.rowRef("A")(elA);

    const removeSpy = vi.spyOn(elA.classList, "remove");
    const addSpy = vi.spyOn(elA.classList, "add");

    // A fresh array with the exact same codes as before — this is the
    // "same entity touched twice in a row" case the hook needs to handle.
    rerender({ touched: ["A"] });

    expect(removeSpy).toHaveBeenCalledWith("row-touched");
    expect(addSpy).toHaveBeenCalledWith("row-touched");
    expect(removeSpy.mock.invocationCallOrder[0]).toBeLessThan(addSpy.mock.invocationCallOrder[0]);
    expect(elA.classList.contains("row-touched")).toBe(true);
  });
});
