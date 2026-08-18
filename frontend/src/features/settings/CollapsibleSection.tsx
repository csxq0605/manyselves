import { useState } from "react";

interface CollapsibleSectionProps {
  readonly children: React.ReactNode;
  readonly defaultOpen?: boolean;
  readonly title: string;
}

export function CollapsibleSection({ children, defaultOpen = false, title }: CollapsibleSectionProps) {
  const [isOpen, setIsOpen] = useState(defaultOpen);

  return (
    <section className="collapsible-section">
      <button
        aria-expanded={isOpen}
        className="collapsible-section__header"
        onClick={() => setIsOpen(!isOpen)}
        type="button"
      >
        <h3 className="collapsible-section__title">{title}</h3>
        <span className="collapsible-section__icon" aria-hidden="true">
          {isOpen ? "▼" : "▶"}
        </span>
      </button>
      {isOpen && (
        <div className="collapsible-section__content">
          {children}
        </div>
      )}
    </section>
  );
}