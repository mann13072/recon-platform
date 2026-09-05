import { z } from "zod";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/v1";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function token(): string {
  if (typeof window !== "undefined") {
    return window.localStorage.getItem("recon.devToken") ?? process.env.NEXT_PUBLIC_DEV_TOKEN ?? "";
  }
  return process.env.NEXT_PUBLIC_DEV_TOKEN ?? "";
}

export function setToken(value: string): void {
  window.localStorage.setItem("recon.devToken", value.trim());
}

async function errorMessage(response: Response): Promise<string> {
  const body: unknown = await response.json().catch(() => null);
  if (body && typeof body === "object" && "detail" in body && typeof body.detail === "string") {
    return body.detail;
  }
  return `${response.status} ${response.statusText}`;
}

export async function api<T>(
  path: string,
  schema: z.ZodType<T>,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  const bearer = token();
  if (bearer) headers.set("Authorization", `Bearer ${bearer}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers, cache: "no-store" });
  if (!response.ok) throw new ApiError(response.status, await errorMessage(response));
  return schema.parse(await response.json());
}

export async function apiJson<T>(
  path: string,
  schema: z.ZodType<T>,
  method: "POST" | "PUT" | "PATCH",
  body: unknown,
): Promise<T> {
  return api(path, schema, { method, body: JSON.stringify(body) });
}

export function can(permissions: readonly string[], permission: string): boolean {
  return permissions.includes(permission);
}
