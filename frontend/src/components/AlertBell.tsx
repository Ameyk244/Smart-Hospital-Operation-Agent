interface AlertBellProps {
  schedulerOn: boolean;
  count: number;
  onOpen: () => void;
}

export function AlertBell({ schedulerOn, count, onOpen }: AlertBellProps) {
  if (!schedulerOn) return null;
  return (
    <button type="button" className="alert-bell" title="Open alerts" onClick={onOpen}>
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0" />
      </svg>
      <span className="sr-only">Alerts</span>
      {count > 0 && (
        <span className="alert-bell-count" aria-label={`${count} open`}>
          {count}
        </span>
      )}
    </button>
  );
}
