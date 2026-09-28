import { type ReactNode } from "react";

export type StatusTone = "neutral" | "positive" | "warning" | "danger" | "info";

export function StatusBadge({
  children,
  tone = "neutral"
}: {
  children: ReactNode;
  tone?: StatusTone;
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function PageHeader({
  action,
  eyebrow,
  title,
  description
}: {
  action?: ReactNode;
  eyebrow: string;
  title: string;
  description: string;
}) {
  return (
    <header className="page-header">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action ? <div className="page-actions">{action}</div> : null}
    </header>
  );
}

export function Section({
  title,
  subtitle,
  action,
  children
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>{title}</h2>
          {subtitle ? <p>{subtitle}</p> : null}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

export function StatCard({
  label,
  value,
  detail,
  tone = "default"
}: {
  label: string;
  value: string;
  detail: string;
  tone?: "default" | "accent";
}) {
  return (
    <article className={`stat-card stat-card-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </article>
  );
}

export type DataColumn<Row extends object> = {
  key: keyof Row;
  label: string;
  render?: (value: Row[keyof Row], row: Row) => ReactNode;
};

export function DataTable<Row extends object>({
  columns,
  rows,
  rowKey
}: {
  columns: DataColumn<Row>[];
  rows: Row[];
  rowKey: keyof Row;
}) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={String(column.key)} scope="col">
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row[rowKey])}>
              {columns.map((column) => {
                const value = row[column.key];
                return (
                  <td key={String(column.key)}>
                    {column.render ? column.render(value, row) : (value as ReactNode)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ProgressBar({ value, label }: { value: number; label: string }) {
  return (
    <div className="progress-block">
      <div>
        <span>{label}</span>
        <strong>{value}%</strong>
      </div>
      <div
        aria-label={`${label}: ${value}%`}
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={value}
        className="progress-track"
        role="progressbar"
      >
        <span style={{ width: `${value}%` }} />
      </div>
    </div>
  );
}

export function EmptyState({
  title,
  description
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="empty-state">
      <span aria-hidden="true">+</span>
      <h3>{title}</h3>
      <p>{description}</p>
    </div>
  );
}

export function RequestState({
  loading,
  error,
  onRetry
}: {
  loading: boolean;
  error: string | null;
  onRetry?: () => void;
}) {
  if (loading) {
    return <div className="request-state" role="status">Loading…</div>;
  }
  if (error) {
    return (
      <div className="request-state request-error" role="alert">
        <span>{error}</span>
        {onRetry ? (
          <button className="button button-secondary" onClick={onRetry} type="button">
            Retry
          </button>
        ) : null}
      </div>
    );
  }
  return null;
}
