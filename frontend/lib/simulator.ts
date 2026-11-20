import type { FleetSnapshotIn, SimDevice } from "@/lib/types";

// mulberry32: a small seeded PRNG, so a given seed always produces the same feed.
export function mulberry32(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
const round1 = (value: number) => Math.round(value * 10) / 10;

type Readings = { disk: number; ram: number; battery: number | null; trend: number };

// Each device gets its own drift rate, so some devices consistently run low while others stay healthy.
export function makeRoster(seed: number, count: number): SimDevice[] {
  const rand = mulberry32(seed);
  return Array.from({ length: count }, (_, index) => {
    const laptop = rand() < 0.7;
    return {
      device_id: `sim-${laptop ? "lap" : "desk"}-${String(index + 1).padStart(3, "0")}`,
      employee_id: `sim-emp-${String(index + 1).padStart(3, "0")}`,
      device_type: laptop ? "laptop" : "desktop",
      os_name: "macOS",
      os_version: rand() < 0.5 ? "14.6" : "15.2"
    };
  });
}

export class TelemetrySimulator {
  private readonly rand: () => number;
  private readonly readings = new Map<string, Readings>();

  constructor(
    private readonly devices: SimDevice[],
    seed: number,
    private readonly source = "simulated-feed"
  ) {
    this.rand = mulberry32(seed);
    for (const device of devices) {
      this.readings.set(device.device_id, {
        disk: 5 + this.rand() * 90,
        ram: 20 + this.rand() * 75,
        battery: device.device_type === "laptop" ? 70 + this.rand() * 30 : null,
        trend: (this.rand() - 0.5) * 2
      });
    }
  }

  get deviceCount(): number {
    return this.devices.length;
  }

  // Advances each device one step and returns a snapshot that includes every device.
  next(observedAt: Date): FleetSnapshotIn {
    return {
      source: this.source,
      observed_at: observedAt.toISOString(),
      devices: this.devices.map((device) => {
        const state = this.readings.get(device.device_id)!;
        state.disk = clamp(state.disk - state.trend * 0.8 + (this.rand() - 0.5), 0, 100);
        state.ram = clamp(state.ram + (this.rand() - 0.5) * 4, 0, 100);
        if (state.battery !== null) {
          // Battery is held steady. The trend fit uses wall-clock time, so any drift per 5-second tick
          // would project end-of-life within days. Real decline needs weeks of history.
          state.battery = clamp(state.battery + (this.rand() - 0.5) * 0.1, 0, 100);
        }
        return {
          device_id: device.device_id,
          employee_id: device.employee_id,
          device_type: device.device_type,
          os_name: device.os_name,
          os_version: device.os_version,
          disk_free_pct: round1(state.disk),
          ram_used_pct: round1(state.ram),
          battery_health_pct: state.battery === null ? null : round1(state.battery),
          compliance: [
            { check_id: "filevault_enabled", severity: "high" as const, passed: this.rand() > 0.1 }
          ]
        };
      })
    };
  }
}
