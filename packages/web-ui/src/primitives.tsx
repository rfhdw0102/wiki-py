"use client";

import { type ReactNode, useId, useMemo, useState } from "react";

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
  description,
  className
}: {
  action?: ReactNode;
  eyebrow?: string;
  title: string;
  description?: string;
  className?: string;
}) {
  return (
    <header className={className ? `page-header ${className}` : "page-header"}>
      <div>
        {eyebrow ? <span className="eyebrow">{eyebrow}</span> : null}
        <h1>{title}</h1>
        {description ? <p>{description}</p> : null}
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
  tone = "default",
  index
}: {
  label: string;
  value: string;
  detail: string;
  tone?: "default" | "accent";
  index?: string;
}) {
  return (
    <article className={`stat-card stat-card-${tone}`}>
      {index ? <span className="stat-index" aria-hidden="true">{index}</span> : null}
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

const DEFAULT_PAGE_SIZE_OPTIONS = [10, 20, 50];

function paginationPages(page: number, pageCount: number): Array<number | string> {
  if (pageCount <= 7) return Array.from({ length: pageCount }, (_, index) => index + 1);
  const start = Math.max(2, Math.min(page - 2, pageCount - 4));
  const end = Math.min(pageCount - 1, Math.max(page + 2, 5));
  const pages: Array<number | string> = [1];
  if (start > 2) pages.push(start === 3 ? 2 : "start-gap");
  for (let value = start; value <= end; value++) pages.push(value);
  if (end < pageCount - 1) pages.push(end === pageCount - 2 ? pageCount - 1 : "end-gap");
  pages.push(pageCount);
  return pages;
}

export function Pagination({
  page,
  pageCount,
  pageSize,
  pageSizeOptions,
  onPageChange,
  onPageSizeChange,
  disabled = false,
  label = "分页"
}: {
  page: number;
  pageCount: number;
  pageSize: number;
  pageSizeOptions: readonly number[];
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <nav aria-label={label} className="pagination-controls">
      <button
        aria-label="上一页"
        disabled={disabled || page <= 1}
        onClick={() => onPageChange(page - 1)}
        type="button"
      >
        <span aria-hidden="true">‹</span>
      </button>
      {paginationPages(page, pageCount).map((value) => typeof value === "number" ? (
        <button
          aria-current={value === page ? "page" : undefined}
          aria-label={`第 ${value} 页`}
          disabled={disabled}
          key={value}
          onClick={() => onPageChange(value)}
          type="button"
        >
          {value}
        </button>
      ) : (
        <span aria-hidden="true" className="pagination-gap" key={value}>…</span>
      ))}
      <button
        aria-label="下一页"
        disabled={disabled || page >= pageCount}
        onClick={() => onPageChange(page + 1)}
        type="button"
      >
        <span aria-hidden="true">›</span>
      </button>
      <select
        aria-label="每页条数"
        disabled={disabled}
        onChange={(event) => onPageSizeChange(Number(event.target.value))}
        value={pageSize}
      >
        {pageSizeOptions.map((size) => (
          <option key={size} value={size}>{size} 条/页</option>
        ))}
      </select>
    </nav>
  );
}

export function DataTable<Row extends object>({
  columns,
  rows,
  rowKey,
  pagination = true,
  searchable = true,
  initialPageSize = 10,
  pageSizeOptions = DEFAULT_PAGE_SIZE_OPTIONS
}: {
  columns: DataColumn<Row>[];
  rows: Row[];
  rowKey: keyof Row;
  pagination?: boolean;
  searchable?: boolean;
  initialPageSize?: number;
  pageSizeOptions?: number[];
}) {
  const pageSizeId = useId();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(initialPageSize);
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLocaleLowerCase("zh-CN");
  const availablePageSizes = useMemo(
    () => Array.from(new Set([...pageSizeOptions, initialPageSize])).sort((a, b) => a - b),
    [initialPageSize, pageSizeOptions]
  );
  const filteredRows = useMemo(() => {
    if (!normalizedQuery) return rows;
    return rows.filter((row) =>
      Object.values(row).some((value) => {
        const text =
          value !== null && typeof value === "object"
            ? JSON.stringify(value) ?? ""
            : String(value ?? "");
        return text.toLocaleLowerCase("zh-CN").includes(normalizedQuery);
      })
    );
  }, [normalizedQuery, rows]);
  const pageCount = Math.max(1, Math.ceil(filteredRows.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const startIndex = pagination ? (currentPage - 1) * pageSize : 0;
  const visibleRows = pagination
    ? filteredRows.slice(startIndex, startIndex + pageSize)
    : filteredRows;
  const showSearch = searchable && pagination && rows.length > availablePageSizes[0];

  return (
    <div className="data-table">
      {showSearch ? (
        <div className="table-tools">
          <input
            aria-label="筛选当前列表"
            onChange={(event) => {
              setQuery(event.target.value);
              setPage(1);
            }}
            placeholder="筛选当前列表…"
            type="search"
            value={query}
          />
          <span>{filteredRows.length} 条结果</span>
        </div>
      ) : null}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {columns.map((column, columnIndex) => (
                <th key={`${String(column.key)}-${columnIndex}`} scope="col">
                  {column.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visibleRows.map((row) => (
              <tr key={String(row[rowKey])}>
                {columns.map((column, columnIndex) => {
                  const value = row[column.key];
                  return (
                    <td key={`${String(column.key)}-${columnIndex}`}>
                      {column.render ? column.render(value, row) : (value as ReactNode)}
                    </td>
                  );
                })}
              </tr>
            ))}
            {visibleRows.length === 0 ? (
              <tr>
                <td className="table-no-results" colSpan={columns.length}>
                  没有匹配的结果
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      {pagination ? (
        <div className="table-pagination">
          <div className="table-page-size">
            <label htmlFor={pageSizeId}>每页</label>
            <select
              id={pageSizeId}
              onChange={(event) => {
                setPageSize(Number(event.target.value));
                setPage(1);
              }}
              value={pageSize}
            >
              {availablePageSizes.map((size) => (
                <option key={size} value={size}>{size} 条</option>
              ))}
            </select>
          </div>
          <span aria-live="polite">
            {filteredRows.length
              ? `${startIndex + 1}–${Math.min(startIndex + pageSize, filteredRows.length)} / ${filteredRows.length}`
              : "0 条"}
          </span>
          <div className="table-page-buttons">
            <button
              aria-label="上一页"
              disabled={currentPage === 1}
              onClick={() => setPage(Math.max(1, currentPage - 1))}
              type="button"
            >
              上一页
            </button>
            <strong>{currentPage} / {pageCount}</strong>
            <button
              aria-label="下一页"
              disabled={currentPage === pageCount}
              onClick={() => setPage(Math.min(pageCount, currentPage + 1))}
              type="button"
            >
              下一页
            </button>
          </div>
        </div>
      ) : null}
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
    return <div className="request-state" role="status"><span className="loading-dot" />正在加载…</div>;
  }
  if (error) {
    return (
      <div className="request-state request-error" role="alert">
        <span>{error}</span>
        {onRetry ? (
          <button className="button button-secondary" onClick={onRetry} type="button">
            重试
          </button>
        ) : null}
      </div>
    );
  }
  return null;
}
