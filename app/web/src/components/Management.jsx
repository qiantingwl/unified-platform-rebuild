import React, { useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { X, Search } from "lucide-react";
import { Button, Notice, nf, useApp } from "../ui";
import { queryString, useDebounced, useQuery } from "../query";

export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  size = "medium",
  busy = false,
}) {
  const returnFocus = useRef(null);
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen && busy) return;
        onOpenChange?.(nextOpen);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="drawerOverlay modalOverlay" />
        <Dialog.Content
          className={`management-modal modal-${size}`}
          aria-busy={busy || undefined}
          onEscapeKeyDown={(event) => {
            if (busy) event.preventDefault();
          }}
          onInteractOutside={(event) => {
            if (busy) event.preventDefault();
          }}
          onOpenAutoFocus={() => {
            returnFocus.current = document.activeElement;
          }}
          onCloseAutoFocus={(event) => {
            if (returnFocus.current?.isConnected) {
              event.preventDefault();
              returnFocus.current.focus();
            }
          }}
        >
          <div className="drawerhead">
            <div>
              <Dialog.Title>{title}</Dialog.Title>
              <Dialog.Description className="drawerDesc">
                {description || "保存后更新当前列表"}
              </Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <button
                type="button"
                className="btn icon ghost"
                aria-label="关闭弹窗"
                disabled={busy}
              >
                <X size={18} />
              </button>
            </Dialog.Close>
          </div>
          <div className="modalbody">{children}</div>
          {footer && <div className="modalfooter">{footer}</div>}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
export function Pagination({
  page,
  pageSize,
  total,
  loading,
  onPage,
  onPageSize,
}) {
  const known = total != null && Number.isFinite(Number(total));
  const pages = known ? Math.max(1, Math.ceil(Number(total) / pageSize)) : null;
  return (
    <div className="tablefoot">
      <span>
        {total == null ? "正在查询记录数" : `共 ${nf(total)} 条`}{" "}
        {loading && " · 正在更新…"}
      </span>
      <div className="row wrap">
        {onPageSize && (
          <select
            aria-label="每页条数"
            value={pageSize}
            onChange={(e) => onPageSize(Number(e.target.value))}
          >
            {[25, 50, 100].map((n) => (
              <option key={n} value={n}>
                {n} 条 / 页
              </option>
            ))}
          </select>
        )}
        <span>
          第 {page} / {pages == null || page > pages ? "—" : pages} 页
        </span>
        <Button
          disabled={loading || page <= 1}
          onClick={() => onPage(page - 1)}
        >
          上一页
        </Button>
        <Button
          disabled={loading || !known || page >= pages}
          onClick={() => onPage(page + 1)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}
export function accountName(a) {
  const label =
    a.display_name || a.original_label || a.original_name || a.label;
  return label && !/迁入验收|副本验收|线上测试|沙盒/.test(label)
    ? label
    : [
        a.platform,
        a.identity_hint ||
          a.external_id_hint ||
          `ID ${a.id || a.account_id || "未记录"}`,
      ]
        .filter(Boolean)
        .join(" · ");
}
export function accountIdentity(a) {
  return (
    [a.identity_hint || a.external_id_hint, a.source_label]
      .filter(Boolean)
      .join(" · ") || `ID ${a.id}`
  );
}
export function AccountPicker({
  name = "account_id",
  platform = "",
  capability = "",
  value = "",
  onChange,
  required = false,
  label = "选择账户",
  revision = 0,
}) {
  const [open, setOpen] = useState(false),
    [q, setQ] = useState(""),
    [page, setPage] = useState(1),
    [selected, setSelected] = useState(null);
  useEffect(() => {
    setPage(1);
    setQ("");
  }, [platform, capability]);
  const search = useDebounced(q);
  const query = useQuery(
    open
      ? `/api/accounts?${queryString({ platform, capability, q: search, page, page_size: 25 })}`
      : null,
    revision,
  );
  const result = query.result;
  return (
    <div className="account-picker">
      <input type="hidden" name={name} value={value} />
      <Button
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
      >
        {selected?.id === value
          ? `${accountName(selected)} · ${accountIdentity(selected)}`
          : value
            ? `账户 ${value}`
            : label}
      </Button>
      {value && !required && (
        <Button
          variant="ghost"
          onClick={() => {
            setSelected(null);
            onChange("");
          }}
        >
          清除
        </Button>
      )}
      <Modal
        open={open}
        onOpenChange={setOpen}
        title={label}
        description="按平台身份、原名称搜索，翻页只读取当前页"
      >
        <div className="toolbar">
          <div className="searchbox">
            <Search size={15} />
            <input
              autoFocus
              aria-label="搜索原始账户身份"
              value={q}
              placeholder="原名称 / UID / 脱敏身份"
              onChange={(e) => {
                setQ(e.target.value);
                setPage(1);
              }}
            />
          </div>
        </div>
        {query.error && (
          <Notice>
            {query.error}
            <Button onClick={query.reload}>重试</Button>
          </Notice>
        )}
        <div className="picker-results" aria-busy={query.loading}>
          {(result?.items || []).map((a) => (
            <button
              type="button"
              className={`picker-option ${a.id === value ? "selected" : ""}`}
              key={a.id}
              onClick={() => {
                setSelected(a);
                onChange(a.id, a);
                setOpen(false);
              }}
            >
              <strong>{accountName(a)}</strong>
              <small>
                {a.platform} · {accountIdentity(a)}
              </small>
            </button>
          ))}
          {!query.loading && !query.error && !result?.items?.length && (
            <div className="noresults">没有匹配的账户，请调整搜索条件。</div>
          )}
        </div>
        <Pagination
          page={page}
          pageSize={25}
          total={result?.total}
          loading={query.loading}
          onPage={setPage}
        />
      </Modal>
    </div>
  );
}

export function ActionMenu({
  title = "操作",
  triggerLabel = "操作",
  children,
  disabled = false,
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button disabled={disabled} onClick={() => setOpen(true)}>
        {triggerLabel}
      </Button>
      <Modal
        open={open}
        onOpenChange={setOpen}
        title={title}
        description="选择要执行的操作"
        size="small"
      >
        <div
          className="action-menu"
          onClick={(e) => {
            if (e.target.closest("button:not(:disabled)")) setOpen(false);
          }}
        >
          {children}
        </div>
      </Modal>
    </>
  );
}
export function subscriptionLabel(source) {
  if (!source) return "来源已移除";
  if (source.name && !/^source-[a-f0-9]+$/i.test(source.name))
    return source.name;
  return source.data?.source_host || source.data?.file_name || "未命名订阅";
}
