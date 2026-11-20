export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

// Pulls FastAPI's `detail` (a string, or an object with a message) into one readable sentence.
export async function readError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") {
      return body.detail;
    }
    if (body.detail && typeof body.detail === "object" && "message" in body.detail) {
      const errors = (body.detail as { errors?: { row: number | null; error: string }[] }).errors ?? [];
      const first = errors[0];
      const where = first?.row != null ? ` (row ${first.row})` : "";
      return `${String((body.detail as { message: unknown }).message)}${first ? `: ${first.error}${where}` : ""}`;
    }
  } catch {
    // Non-JSON error bodies fall through to the generic message below.
  }
  return `Request failed with status ${response.status}.`;
}
