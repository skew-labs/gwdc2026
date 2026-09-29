import type { ReactNode } from "react";
import { AlertCircle, Check, Info, LoaderCircle } from "lucide-react";
import { human } from "../lib/format";
import type { Role } from "../api/contracts";
export function Avatar({
  role = "alpha",
  small = false,
}: {
  role?: Role;
  small?: boolean;
}) {
  return (
    <span
      className={`avatar avatar-${role} ${small ? "small" : ""}`}
      aria-hidden="true"
    >
      <span className="eyes" />
    </span>
  );
}
export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "good" | "warn" | "bad";
}) {
  return <span className={`badge ${tone}`}>{children}</span>;
}
export function Status({ value }: { value: string }) {
  const tone = [
    "CONFIRMED",
    "POSITION_RECONCILED",
    "PERFORMANCE_TRACKED",
    "PASS",
    "APPROVED",
  ].includes(value)
    ? "good"
    : ["FAILED", "DISPUTED", "FAIL"].includes(value)
      ? "bad"
      : [
            "SUBMISSION_UNKNOWN",
            "STALE",
            "EXPIRED",
            "PARTIALLY_COMPLETED",
          ].includes(value)
        ? "warn"
        : "neutral";
  return <Badge tone={tone}>{human(value)}</Badge>;
}
export function Button({
  children,
  onClick,
  disabled = false,
  secondary = false,
  type = "button",
  className = "",
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { secondary?: boolean }) {
  return (
    <button
      type={type}
      className={`${secondary ? "secondary" : "primary"} ${className}`}
      onClick={onClick}
      disabled={disabled}
      {...rest}
    >
      {children}
    </button>
  );
}
export function Row({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="detail-row">
      <span>{label}</span>
      <strong>{children}</strong>
    </div>
  );
}
export function Empty({
  icon,
  title,
  children,
}: {
  icon?: ReactNode;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      {icon || <Info size={24} />}
      <h3>{title}</h3>
      {children && <p>{children}</p>}
    </div>
  );
}
export function Alert({
  children,
  tone = "info",
}: {
  children: ReactNode;
  tone?: "info" | "error" | "success";
}) {
  return (
    <div
      className={`alert ${tone}`}
      role={tone === "error" ? "alert" : "status"}
    >
      {tone === "error" ? (
        <AlertCircle />
      ) : tone === "success" ? (
        <Check />
      ) : (
        <Info />
      )}
      <div>{children}</div>
    </div>
  );
}
export function Spinner({ label = "Loading workspace" }: { label?: string }) {
  return (
    <div className="loading" role="status">
      <LoaderCircle className="spin" />
      {label}
    </div>
  );
}
