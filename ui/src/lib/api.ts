/**
 * The server's answers, typed.
 *
 * ## What changed, and why it matters
 *
 * This file replaces the browser's copy of the domain. There used to be a
 * ledger here: `sessions.ts` parsed CSV text, `kit.ts` found the purchase in
 * each life, `ledger.ts` decomposed the curve, `annotations.ts` found the
 * break-even crossing, `metrics.ts` and `correlation.ts` did the analysis. All
 * of it ran again on every page load, over every match, in every open tab.
 *
 * All of it now lives in `profitdog/server/domain/`, in one implementation,
 * versioned by ruleset and checked against the same recorded sessions the
 * TypeScript was. Two implementations of one accounting disagree eventually;
 * there is one now, and this file is the boundary where its answers arrive.
 *
 * ## Shaping, not deriving
 *
 * The adapters below rename fields and parse dates. That is all they do. No
 * arithmetic, no inference, no filling in of a missing value — if a figure is
 * not in the response, it does not appear on screen. The line to watch for in
 * review is any `+`, `-` or `?:` that invents a number: that is the domain
 * creeping back in, and it belongs on the other side of the wire.
 *
 * The shapes the chart components consume were kept as they were, so several
 * hundred lines of carefully-tuned drawing code did not have to be rewritten
 * to read the same values under different names.
 */

export interface Unassigned {
  positive: number
  negative: number
  total: number
  count: number
}

/** One match, as the list and the history page see it. */
export interface MatchSummary {
  match_key: string
  started_at: string
  ended_at: string | null
  map: string | null
  map_display: string | null
  faction: string | null
  faction_display: string | null
  build: string | null
  live: boolean
  /** Which ruleset read this match. Shown so a reader can tell when it differs. */
  ruleset: string
  lives: number
  duration_sec: number
  kit_cost: number
  earned: number
  other_outflow: number
  spent: number
  unassigned: Unassigned
  profit: number
}

export interface LifeDto {
  id: string
  match_key: string
  match_started_at: string
  started_at: string | null
  map: string | null
  map_display: string | null
  build: string | null
  life_number: number
  kit_cost: number
  earned: number
  other_outflow: number
  spent: number
  profit: number
  duration_sec: number
  has_kit: boolean
  break_even_sec: number | null
  cost_confidence: "derived" | "manual" | "unknown"
}

export interface SpendDto {
  at_sec: number
  amount: number
  after_kit_sec: number
}

export interface KitMarkDto {
  life: number
  baseline: number
  baseline_at_sec: number
  floor: number
  purchase_at_sec: number
  break_even_at_sec: number | null
  start_sec: number
  end_sec: number
  spends: SpendDto[]
}

export interface AdjustmentDto {
  id: string
  from_sec: number
  to_sec: number
  amount: number
  kind: "terminal_adjustment" | "map_transition" | "unclassified"
  confidence: "observed" | "inferred" | "unknown"
  source: string
  /** The recorded `game_state` that justified the classification, if any. */
  rp_state: string | null
  near_life: number | null
  prev_map: string | null
  next_map: string | null
}

export interface MatchDetail extends MatchSummary {
  lives_detail: LifeDto[]
  adjustments: AdjustmentDto[]
  curve: { initial: number; final: number; net: number }
  kit_marks: KitMarkDto[]
}

export interface CurvePoint {
  /** Seconds since the match started. */
  t: number
  /** Money, as the game reports it. */
  c: number
  /** Which life this reading belongs to. */
  l: number
}

export interface CurveResponse {
  match_key: string
  points: CurvePoint[]
  stored: number
  returned: number
  /** True when the server reduced the curve to fit. Said so the chart can say so. */
  downsampled: boolean
}

export interface Totals {
  matches: number
  lives: number
  duration_sec: number
  kit_cost: number
  earned: number
  other_outflow: number
  spent: number
  unassigned: Unassigned
  profit: number
}

export interface MatchesResponse {
  matches: MatchSummary[]
  totals: Totals
  covered: { from: string; to: string } | null
  best: string | null
  filters: {
    maps: { value: string; label: string }[]
    builds: string[]
    has_unknown_build: boolean
    applied: Record<string, string>
  }
  available: number
}

export interface HistoryBucket extends Totals {
  key: string
  label: string
  started_at: string
  match_keys: string[]
  cumulative_profit: number
  map: string | null
  build: string | null
}

export interface HistoryResponse {
  buckets: HistoryBucket[]
  markers: { bucket_index: number; build: string; label: string }[]
  totals: Totals
  covered: { from: string; to: string } | null
  best: string | null
  grouping: string
}

export interface MetricDto {
  key: string
  label: string
  unit: "money" | "duration" | "rate"
  phrase: string
  hint: string
}

export interface MetricsResponse {
  metrics: MetricDto[]
  presets: { id: string; label: string; x: string; y: string }[]
}

export interface AnalysisPoint extends LifeDto {
  x: number
  y: number
}

export interface AnalysisResponse {
  x: MetricDto
  y: MetricDto
  /** Whether an `x = y` reference line means anything for this pairing. */
  comparable: boolean
  points: AnalysisPoint[]
  correlation: {
    r: number | null
    n: number
    trend: { slope: number; intercept: number } | null
  }
  summary: {
    value: string | null
    reading: string
    caveat: string | null
    direction: "up" | "down" | "flat" | "unknown"
  }
  excluded: {
    count: number
    total: number
    groups: { id: string; count: number; phrase: string }[]
    summary: string | null
  }
  matches: number
}

export interface XpResponse {
  totals: Record<string, number>
  by_match: Record<string, Record<string, number>>
  unattributed: Record<string, number>
}

export interface Health {
  ok: boolean
  schema: number
  rulesets: string[]
  seq: number
  oldest_seq: number
  matches: number
  samples: number
  cache: { rows: number; matches: number; hits: number; misses: number }
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

/**
 * Did this error mean the server was unreachable, or that it answered?
 *
 * A 404 is an answer: the server was reached and said no. Reporting it as a
 * connectivity failure sent readers looking for a dead server that was in fact
 * serving every other request on the page.
 */
export function isUnreachable(error: Error | null): boolean {
  return error !== null && !(error instanceof ApiError)
}

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

/**
 * Same origin by design: the server serves this bundle.
 *
 * `VITE_API` exists for `pnpm dev`, where Vite is on one port and the server on
 * another. In production the page and its data come from the same place, which
 * is what makes the WebSocket URL derivable rather than configured.
 */
const BASE = (import.meta.env?.VITE_API as string | undefined) ?? ""

/**
 * Leave for the sign-in page, once.
 *
 * A signed-out tab usually discovers it on several requests at the same time —
 * the page, its curve, the metric catalogue — and each would otherwise start
 * its own navigation. The guard makes the first one win, and carries where we
 * were so the redirect comes back to it.
 */
let leaving = false

function signInAgain(): never {
  if (!leaving) {
    leaving = true
    const here = window.location.pathname + window.location.search
    window.location.href = `/login?next=${encodeURIComponent(here)}`
  }
  // Thrown rather than returned: the navigation is not instant, and a caller
  // that carried on would render an empty page in the meantime.
  throw new ApiError("signed out", 401)
}

export async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    signal,
    headers: { accept: "application/json" },
    // The session cookie is HttpOnly, so it travels on the request and is
    // never readable here. Same-origin in production; explicit for the dev
    // proxy, where the UI and the server are on different ports.
    credentials: "include",
  })
  if (response.status === 401) signInAgain()
  if (!response.ok) {
    throw new ApiError(`${path} returned ${response.status}`, response.status)
  }
  return (await response.json()) as T
}

export async function put<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
  })
  if (response.status === 401) signInAgain()
  if (!response.ok) {
    throw new ApiError(`${path} returned ${response.status}`, response.status)
  }
  return (await response.json()) as T
}

export function liveUrl(since: number | null): string {
  if (BASE) {
    const url = new URL(BASE)
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:"
    url.pathname = "/api/live"
    if (since !== null) url.searchParams.set("since", String(since))
    return url.toString()
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:"
  const query = since !== null ? `?since=${since}` : ""
  return `${protocol}//${window.location.host}/api/live${query}`
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

/** Filters travel as a query string, spelled the way the server reads them. */
export function filterQuery(params: Record<string, string | null | undefined>): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value != null && value !== "") query.set(key, value)
  }
  const text = query.toString()
  return text ? `?${text}` : ""
}

export const api = {
  health: (signal?: AbortSignal) => get<Health>("/api/health", signal),
  matches: (query: string, signal?: AbortSignal) =>
    get<MatchesResponse>(`/api/matches${query}`, signal),
  match: (key: string, signal?: AbortSignal) =>
    get<MatchDetail>(`/api/matches/${encodeURIComponent(key)}`, signal),
  curve: (key: string, maxPoints: number, signal?: AbortSignal) =>
    get<CurveResponse>(
      `/api/matches/${encodeURIComponent(key)}/curve?max_points=${maxPoints}`,
      signal
    ),
  history: (query: string, signal?: AbortSignal) =>
    get<HistoryResponse>(`/api/history${query}`, signal),
  analysis: (query: string, signal?: AbortSignal) =>
    get<AnalysisResponse>(`/api/analysis${query}`, signal),
  metrics: (signal?: AbortSignal) => get<MetricsResponse>("/api/metrics", signal),
  xp: (signal?: AbortSignal) => get<XpResponse>("/api/xp", signal),
  setOverride: (key: string, life: number, value: number | null) =>
    put<{ seq: number; life: number; value: number | null }>(
      `/api/matches/${encodeURIComponent(key)}/overrides/${life}`,
      { value }
    ),
}

// ---------------------------------------------------------------------------
// Shaping for the chart components
// ---------------------------------------------------------------------------

export function parseDate(value: string | null): Date | null {
  if (!value) return null
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

/** The money curve, in the shape `BalanceChart` has always drawn. */
export interface ServerSession {
  matchKey: string
  label: string
  map: string | null
  faction: string | null
  startedAt: Date | null
  points: { elapsedSec: number; value: number; life: number }[]
  lifeStarts: number[]
  peak: number
  trough: number
  final: number
  /** True when the server reduced the curve to fit the chart. */
  downsampled: boolean
}

export function toSession(
  detail: MatchDetail,
  curve: CurveResponse
): ServerSession {
  const points = curve.points.map((p) => ({
    elapsedSec: p.t,
    value: p.c,
    life: p.l,
  }))
  const lifeStarts: number[] = []
  for (let i = 1; i < points.length; i++) {
    if (points[i].life > points[i - 1].life) lifeStarts.push(points[i].elapsedSec)
  }
  const values = points.map((p) => p.value)
  return {
    matchKey: detail.match_key,
    label: detail.map_display ?? detail.match_key,
    map: detail.map_display ?? detail.map,
    faction: detail.faction_display ?? detail.faction,
    startedAt: parseDate(detail.started_at),
    points,
    lifeStarts,
    peak: values.length ? Math.max(...values) : 0,
    trough: values.length ? Math.min(...values) : 0,
    final: detail.curve.final,
    downsampled: curve.downsampled,
  }
}

/** One life plus the marks the chart draws for it. Positions only. */
export interface LifeAnnotation {
  lifeNumber: number
  label: string
  life: LifeDto
  lifeStartSec: number
  lifeEndSec: number
  hasKit: boolean
  baseline: number
  trough: number
  invested: number
  purchaseFromSec: number
  purchaseAtSec: number
  spends: SpendMark[]
  breakEvenAtSec: number | null
  breakEvenInLifeSec: number | null
  guideToSec: number
}

export interface SpendMark {
  id: string
  lifeNumber: number
  atSec: number
  amount: number
  afterKitSec: number
  /** Balance before the outgoing — the top of the drop. */
  fromValue: number
  /** Balance after it — the bottom. */
  toValue: number
}

export interface AdjustmentMark {
  id: string
  adjustment: AdjustmentDto
  fromValue: number
  toValue: number
  isMapBoundary: boolean
}

export interface ChartAnnotations {
  lives: LifeAnnotation[]
  adjustments: AdjustmentMark[]
}

/**
 * The server's marks, renamed for the chart and anchored to the curve.
 *
 * Every *figure* came out of `detail` — the server decided what a kit cost,
 * which outgoings were not part of it and when a life broke even. What is
 * worked out here is where those marks sit on the line, which needs the curve
 * and is a question about drawing rather than about money.
 *
 * Positions are anchored to real readings, never interpolated between two of
 * them: a mark halfway between two polls would be pointing at a balance the
 * game never reported.
 */
export function toAnnotations(
  detail: MatchDetail,
  curve: CurveResponse
): ChartAnnotations {
  const points = curve.points
  const valueAt = (sec: number): number => {
    let value = points[0]?.c ?? 0
    for (const point of points) {
      if (point.t > sec) break
      value = point.c
    }
    return value
  }
  const valueBefore = (sec: number): number => {
    let previous = points[0]?.c ?? 0
    for (const point of points) {
      if (point.t >= sec) break
      previous = point.c
    }
    return previous
  }

  const byLife = new Map(detail.lives_detail.map((l) => [l.life_number, l]))
  const lives: LifeAnnotation[] = []
  for (const mark of detail.kit_marks) {
    const life = byLife.get(mark.life)
    if (!life) continue
    lives.push({
      lifeNumber: mark.life,
      label: `L${mark.life}`,
      life,
      lifeStartSec: mark.start_sec,
      lifeEndSec: mark.end_sec,
      hasKit: life.has_kit,
      baseline: mark.baseline,
      trough: mark.floor,
      invested: life.kit_cost,
      purchaseFromSec: mark.baseline_at_sec,
      purchaseAtSec: mark.purchase_at_sec,
      spends: mark.spends.map((s, i) => ({
        id: `spend-${mark.life}-${i}`,
        lifeNumber: mark.life,
        atSec: s.at_sec,
        amount: s.amount,
        afterKitSec: s.after_kit_sec,
        fromValue: valueBefore(s.at_sec),
        toValue: valueAt(s.at_sec),
      })),
      breakEvenAtSec: mark.break_even_at_sec,
      breakEvenInLifeSec:
        mark.break_even_at_sec === null
          ? null
          : mark.break_even_at_sec - mark.start_sec,
      guideToSec: mark.break_even_at_sec ?? mark.end_sec,
    })
  }
  return {
    lives,
    adjustments: detail.adjustments.map((adjustment) => ({
      id: adjustment.id,
      adjustment,
      fromValue: valueAt(adjustment.from_sec),
      toValue: valueAt(adjustment.to_sec),
      isMapBoundary:
        adjustment.kind === "map_transition" ||
        (!!adjustment.prev_map &&
          !!adjustment.next_map &&
          adjustment.prev_map !== adjustment.next_map),
    })),
  }
}
