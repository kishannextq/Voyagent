import { accessToken } from "@/lib/auth";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");
export const GITHUB_URL = process.env.NEXT_PUBLIC_GITHUB_URL || "https://github.com/websitecreatr99/Voyagent";

export type ServiceId = "llm" | "flights" | "hotels" | "weather";
export type ServiceModes = Record<ServiceId, "live" | "mock">;

export type NodeId =
  | "supervisor"
  | "flight_agent"
  | "hotel_agent"
  | "weather_agent"
  | "budget_agent"
  | "itinerary_agent"
  | "human_approval"
  | "final_response";

export interface TravelState {
  user_query?: string;
  trip_constraints?: Record<string, unknown>;
  selected_agents?: string[];
  supervisor_reasoning?: string;
  flight_results?: string;
  hotel_results?: string;
  weather_results?: string;
  budget_results?: string;
  itinerary?: string;
  approval_request?: string;
  human_feedback?: string;
  approved?: boolean;
  final_response?: string;
  llm_calls?: number;
}

export interface InterruptPayload {
  question?: string;
  draft_itinerary?: string;
  approval_request?: string;
}

export type PlanStatus = "awaiting_approval" | "completed" | "blocked";

export interface PlanResponse {
  thread_id: string;
  status: PlanStatus;
  state: TravelState;
  interrupt: InterruptPayload | null;
  modes?: ServiceModes | null;
}

export type StreamEvent =
  | { event: "start"; data: { thread_id: string; modes?: ServiceModes } }
  | { event: "node"; data: { node: NodeId; data: TravelState } }
  | { event: "interrupt"; data: { node: "human_approval"; data: InterruptPayload } }
  | { event: "done"; data: PlanResponse }
  | { event: "error"; data: { message: string } };

/** POST a JSON body and parse the Server-Sent Events response stream. */
async function streamSSE(path: string, body: unknown, onEvent: (e: StreamEvent) => void, signal?: AbortSignal) {
  const headers: Record<string, string> = { "Content-Type": "application/json", Accept: "text/event-stream" };
  const token = await accessToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    signal,
  });

  if (res.status === 401) throw new Error("Please sign in to plan a trip (your session may have expired).");
  if (res.status === 429 || res.status === 503) {
    const wait = res.headers.get("Retry-After");
    const why = res.status === 429 ? "You're sending requests too quickly." : "The server is busy right now.";
    throw new Error(`${why} Please try again ${wait ? `in ${wait}s` : "shortly"}.`);
  }
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j?.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {}
    throw new Error(detail);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const flush = (block: string) => {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return;
    try {
      onEvent({ event, data: JSON.parse(dataLines.join("\n")) } as StreamEvent);
    } catch {
      /* ignore malformed frame */
    }
  };

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      flush(buffer.slice(0, idx));
      buffer = buffer.slice(idx + 2);
    }
  }
  if (buffer.trim()) flush(buffer);
}

export function streamPlan(
  body: { query: string; thread_id?: string | null },
  onEvent: (e: StreamEvent) => void,
  signal?: AbortSignal,
) {
  return streamSSE("/api/v1/plan/stream", body, onEvent, signal);
}

export function streamApproval(
  threadId: string,
  body: { approved: boolean; feedback: string },
  onEvent: (e: StreamEvent) => void,
  signal?: AbortSignal,
) {
  return streamSSE(`/api/v1/plan/${encodeURIComponent(threadId)}/approve/stream`, body, onEvent, signal);
}

export interface Health {
  ok: boolean;
  /** Whether the next plan will use live services or demo data. */
  modes?: ServiceModes;
}

export async function checkHealth(): Promise<Health> {
  try {
    const res = await fetch(`${API_URL}/api/v1/health`, { cache: "no-store" });
    if (!res.ok) return { ok: false };
    const j = await res.json().catch(() => ({}));
    return { ok: true, modes: j?.modes };
  } catch {
    return { ok: false };
  }
}
